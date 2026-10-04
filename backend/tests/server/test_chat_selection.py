"""The Recursos selection of a workspace chat message (#433): `selected_source_ids` validated on
the body, shown to the classifier as marked, the referent of `targets`, the ask-back when nothing
is selected, the selected pages first in an editor turn, echoed on the stream and kept on the
persisted request (a confirmation past the cap and a replay keep it).

Every test runs the real app (`FakeClaude`, `tmp_vault`, the FastAPI test client) with the
observer loop off, so the scripted replies are, in order, the classifier's (role `observer`) and
the editor's. Every wait is bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic
from multi_source_topic import NOTES_ID, PDF_ID, PDF_PAGE_ID, make_multi_source_topic
from revise_topic import make_revise_topic
from studentassistant.config import (
    EditorSettings,
    LlmSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
    SourcesSettings,
)
from studentassistant.editor.revise import EDIT_TOOL, NO_SELECTION_NOTE
from studentassistant.llm import FakeClaude
from studentassistant.observer import ASSISTANT_REQUEST_KIND, AssistantRequest
from studentassistant.observer.requests import TOOL_NAME, ReportedRequest
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.sources import triage_status
from studentassistant.vault import Vault, read_topic_events

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
PAGE = "[Apuntes, página {n}](../sources/notes/page-00{n}.jpg)"
PAGE_1 = "sources/notes/page-001.jpg"
PAGE_2 = "sources/notes/page-002.jpg"
ASK_BACK = "¿De qué páginas hablas? Selecciónalas en Recursos o dime su número."
AppFactory = Callable[..., FastAPI]


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(
        transport: FakeClaude | None,
        *,
        max_selected_sources: int = 20,
        llm: LlmSettings | None = None,
    ) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            sources=SourcesSettings(transcription_enabled=False),
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False, max_selected_sources=max_selected_sources),
                llm=llm or LlmSettings(),
                editor=EditorSettings(doubts_in_chat=False, incorporate_batch_size=1),
            ),
        )

    return make


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _client(make_app(fake)) as client:
        yield client


def _url(topic: GenerateTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/workspace/messages"


def _settle(client: TestClient) -> None:
    app: Any = client.app
    consumer: AssistantRequestConsumer = app.state.assistant_requests
    client.portal.call(consumer.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]


def _subscribe(client: TestClient, topic: GenerateTopic) -> WorkspaceSubscription:
    app: Any = client.app
    return app.state.workspace.subscribe(topic.subject, topic.topic)  # type: ignore[no-any-return]


def _names(events: list[WorkspaceEvent]) -> list[str]:
    return [event.event for event in events if event.event != "reply.delta"]


def _replies(events: list[WorkspaceEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.event == "reply.delta")


def _classify(fake: FakeClaude, *requests: dict[str, Any]) -> None:
    fake.reply_tool(
        TOOL_NAME,
        {"requests": [{"segment_ids": ["m1"], **request} for request in requests]},
    )


def _classifier_text(fake: FakeClaude) -> str:
    request = fake.requests[0]
    assert request.role == "observer"
    return str(request.messages[0]["content"][0]["text"])


def _editor_texts(fake: FakeClaude) -> list[str]:
    """The text blocks of the editor call's first user message, in order."""
    [request] = [r for r in fake.requests if r.role == "editor"]
    return [block["text"] for block in request.messages[0]["content"] if block["type"] == "text"]


def _requests_written(topic: GenerateTopic) -> list[dict[str, Any]]:
    return [
        event.payload
        for _sid, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if event.kind == ASSISTANT_REQUEST_KIND
    ]


# -- the body ------------------------------------------------------------------------------------


def test_a_selection_outside_the_topic_or_too_long_is_refused(
    make_app: AppFactory, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_topic(user_vault)
    with _client(make_app(fake, max_selected_sources=2)) as client:
        url = _url(topic)
        unknown = client.post(
            url, json={"text": "incorpora esto", "selected_source_ids": ["sources/notes/x.jpg"]}
        )
        assert unknown.status_code == 422
        assert unknown.json()["detail"].startswith("La selección incluye algo que no es una")
        assert "sources/notes/x.jpg" in unknown.json()["detail"]
        # A page fragment only names a PDF's page.
        fragment = client.post(
            url, json={"text": "esto", "selected_source_ids": [f"{PAGE_1}#page=1"]}
        )
        assert fragment.status_code == 422
        many = client.post(
            url,
            json={"text": "esto", "selected_source_ids": [PAGE_1, PAGE_2, "sources/notes/3.jpg"]},
        )
        assert many.status_code == 422 and "como mucho 2" in many.json()["detail"]
        with_confirm = client.post(
            url,
            json={"confirm_over_cap": True, "turn_id": "turn-x", "selected_source_ids": [PAGE_1]},
        )
        assert with_confirm.status_code == 422
        assert fake.requests == []  # nothing was classified


def test_a_message_without_a_selection_is_as_before_and_says_nothing_is_selected(
    client: TestClient, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_revise_topic(user_vault)
    _classify(fake, {"kind": "edit", "summary": "Poner la definición en negrita"})
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Vale.")
    response = client.post(_url(topic), json={"text": "pon la definición en negrita"})
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert "selected_source_ids" not in request or request["selected_source_ids"] == []
    _settle(client)
    assert "Selected by the student now (in Recursos): nothing." in _classifier_text(fake)
    assert "selected_source_ids" not in _requests_written(topic)[0]


# -- the referent --------------------------------------------------------------------------------


def test_the_selection_is_the_referent_of_an_incorporation(
    client: TestClient, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_topic(user_vault)
    subscription = _subscribe(client, topic)
    _classify(
        fake,
        {
            "kind": "incorporate",
            "summary": "Incorporar las páginas 2 y 1",
            "targets": [PAGE_2, PAGE_1],
        },
    )
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Incorporo las páginas 1 y 2",
            "ops": [
                {
                    "op": "add_section",
                    "after": "",
                    "level": 2,
                    "title": "Definición",
                    "anchor": "definicion",
                    "text": "Lo de las páginas.[^p1][^p2]",
                }
            ],
            "footnotes": [
                {"label": "p1", "definition": PAGE.format(n=1)},
                {"label": "p2", "definition": PAGE.format(n=2)},
            ],
        },
        text="Incorporo las dos páginas.",
    )

    response = client.post(
        _url(topic),
        json={
            "text": "incorpora el texto de estas",
            "selected_source_ids": [PAGE_2, PAGE_1, PAGE_2],  # a repeat is dropped
        },
    )
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert request["kind"] == "incorporate" and request["targets"] == [PAGE_2, PAGE_1]
    assert request["selected_source_ids"] == [PAGE_2, PAGE_1]
    _settle(client)

    text = _classifier_text(fake)
    assert (
        "Selected by the student now (in Recursos), in order:\n"
        f"- {PAGE_2}: página 2 (apuntes) -- pendiente\n"
        f"- {PAGE_1}: página 1 (apuntes) -- pendiente"
    ) in text
    assert text.index("Selected by the student now") < text.index("m1 incorpora el texto")
    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result", "notes.changed"]
    assert events[0].data["selected_source_ids"] == [PAGE_2, PAGE_1]
    result = next(e.data for e in events if e.event == "turn.result")
    assert result["applied"] is True and sorted(result["source_ids"]) == [PAGE_1, PAGE_2]
    [written] = _requests_written(topic)
    assert written["selected_source_ids"] == [PAGE_2, PAGE_1]


def test_nothing_selected_and_nothing_named_asks_back_in_spanish(
    client: TestClient, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_revise_topic(user_vault)
    subscription = _subscribe(client, topic)
    _classify(fake, {"kind": "question", "summary": ASK_BACK})
    fake.reply_text(ASK_BACK)

    response = client.post(_url(topic), json={"text": "incorpora el texto de esta captura"})
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert request["kind"] == "question" and request["summary"] == ASK_BACK
    _settle(client)

    assert "Selected by the student now (in Recursos): nothing." in _classifier_text(fake)
    assert any(NO_SELECTION_NOTE in text for text in _editor_texts(fake))
    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result"]
    assert events[0].data["summary"] == ASK_BACK
    assert "selected_source_ids" not in events[0].data
    assert _replies(events) == ASK_BACK


def test_an_edit_gets_the_selected_pages_first_marked_with_their_images(
    client: TestClient, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_revise_topic(user_vault)
    _classify(fake, {"kind": "edit", "summary": "Reescribir con el texto de la captura"})
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Lo reescribo.")

    response = client.post(
        _url(topic),
        json={
            "text": "reescribe esto con el texto de la captura",
            "selected_source_ids": [PAGE_1],
        },
    )
    assert response.status_code == 202, response.text
    _settle(client)

    [editor] = [r for r in fake.requests if r.role == "editor"]
    content = editor.messages[0]["content"]
    heading = next(
        i
        for i, block in enumerate(content)
        if block["type"] == "text" and block["text"].startswith("## Selección actual")
    )
    # After the cached topic, then the selected page with its transcription and image, then the
    # student's message.
    assert heading > 0 and content[heading - 1].get("cache_control") is not None
    page = content[heading + 1]
    assert page["text"].startswith(f"### Seleccionada: Apuntes, página 1 ({PAGE_1})")
    assert "Derivada: límite del cociente incremental." in page["text"]
    assert content[heading + 2]["type"] == "image"
    assert content[-1]["text"].startswith("## Conversación hasta ahora")
    assert "«Selección actual del estudiante»" in content[-1]["text"]


def test_a_selected_pdf_cannot_be_set_aside_and_the_rest_is(
    client: TestClient, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_multi_source_topic(user_vault)
    subscription = _subscribe(client, topic)  # type: ignore[arg-type]
    _classify(
        fake,
        {
            "kind": "set_aside",
            "summary": "Apartar lo seleccionado",
            "targets": [NOTES_ID, PDF_PAGE_ID],
        },
    )
    response = client.post(
        _url(topic),  # type: ignore[arg-type]
        json={"text": "apártalas", "selected_source_ids": [NOTES_ID, PDF_PAGE_ID]},
    )
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert request["targets"] == [NOTES_ID, PDF_ID]  # a PDF page names its PDF
    assert request["selected_source_ids"] == [NOTES_ID, PDF_PAGE_ID]
    _settle(client)

    assert f"- {PDF_PAGE_ID}: page 1 of {PDF_ID}:" in _classifier_text(fake)
    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result"]
    reply = _replies(events)
    assert reply.startswith("He apartado la página 1.")
    assert "no se puede apartar: solo se apartan y recuperan páginas capturadas" in reply
    triage = triage_status(topic.vault, topic.subject, topic.topic)
    assert triage[NOTES_ID].set_aside
    assert PDF_ID not in triage or not triage[PDF_ID].set_aside


def test_a_pdf_named_without_a_selection_is_still_refused_for_a_set_aside(
    make_app: AppFactory, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_multi_source_topic(user_vault)
    with _client(make_app(fake, llm=LlmSettings(structured_reasks=0))) as client:
        _classify(fake, {"kind": "set_aside", "summary": "Apartar el PDF", "targets": [PDF_ID]})
        fake.reply_text("Vale.")  # the kept text, answered by the editor
        response = client.post(_url(topic), json={"text": "aparta el pdf"})  # type: ignore[arg-type]
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["classified"] is False and body["requests"][0]["kind"] == "edit"
        _settle(client)
        assert fake.pending == 0


# -- kept for a confirmation and a replay --------------------------------------------------------


class _ScriptedClassifier:
    def __init__(self, *requests: ReportedRequest) -> None:
        self.requests = list(requests)
        self.selected: list[list[str]] = []

    async def classify(self, *_args: Any, selected: Any = (), **_kwargs: Any) -> list[Any]:
        self.selected.append(list(selected))
        return self.requests


def test_an_edit_stopped_at_the_cap_keeps_its_selection_when_confirmed(
    make_app: AppFactory, fake: FakeClaude, user_vault: Vault
) -> None:
    topic = make_revise_topic(user_vault)
    app = make_app(fake, llm=LlmSettings(max_usd_per_day=0))
    with _client(app) as client:
        classifier = _ScriptedClassifier(
            ReportedRequest(kind="edit", summary="Reescribir esto", segment_ids=["m1"])
        )
        app.state.message_classifier = classifier
        subscription = _subscribe(client, topic)
        posted = client.post(
            _url(topic), json={"text": "reescribe esto", "selected_source_ids": [PAGE_2]}
        )
        assert posted.status_code == 202, posted.text
        assert classifier.selected == [[PAGE_2]]
        _settle(client)
        error = subscription.drain()[-1]
        assert error.event == "turn.error" and error.data["code"] == "cost_cap_reached"

        fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Hecho.")
        confirmed = client.post(
            _url(topic), json={"confirm_over_cap": True, "turn_id": error.data["turn_id"]}
        )
        assert confirmed.status_code == 202, confirmed.text
        assert confirmed.json()["requests"][0]["selected_source_ids"] == [PAGE_2]
        _settle(client)
        texts = _editor_texts(fake)
        assert any(t.startswith(f"### Seleccionada: Apuntes, página 2 ({PAGE_2})") for t in texts)


def test_the_persisted_request_round_trips_its_selection() -> None:
    fields = {
        "request_id": "req-t1",
        "kind": "edit",
        "summary": "Reescribir esto",
        "text": "reescribe esto",
        "t_start_ms": 0,
        "t_end_ms": 0,
        "detector": "typed",
    }
    old = AssistantRequest.model_validate(fields)  # written before #433
    assert old.selected_source_ids == [] and "selected_source_ids" not in old.payload()
    new = AssistantRequest.model_validate({**fields, "selected_source_ids": [PDF_PAGE_ID, PAGE_1]})
    payload = new.payload()
    assert payload["selected_source_ids"] == [PDF_PAGE_ID, PAGE_1]
    assert AssistantRequest.model_validate(payload) == new
