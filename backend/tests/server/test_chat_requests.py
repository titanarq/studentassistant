"""Chat-driven requests (#327): typed workspace messages classified like spoken ones, and the
dispatch of `incorporate`, `set_aside`, `restore` and `doubt_answer` in the request consumer.

Every test runs the real app (`FakeClaude`, `tmp_vault`, the FastAPI test client). The observer
loop stays off, so the scripted replies are, in order, the classifier's (role `observer`) and the
editor's. Every wait is bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import (
    EditorSettings,
    LlmSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
    SourcesSettings,
)
from studentassistant.editor.doubts import DECISION_TOOL, REVIEW_TOOL
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude, LLMAPIError
from studentassistant.observer import ASSISTANT_REQUEST_KIND
from studentassistant.observer.requests import TOOL_NAME
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.doubt_chat import DoubtChat
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.sources import CAPTURE_TRIAGED_KIND, triage_status
from studentassistant.vault import (
    Vault,
    list_sessions,
    put_source,
    read_notes,
    read_topic_events,
    sources_directory,
)

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
PAGE = "[Apuntes, página {n}](../sources/notes/page-00{n}.jpg)"
PAGE_1 = "sources/notes/page-001.jpg"
PAGE_2 = "sources/notes/page-002.jpg"
PAGE_3 = "sources/notes/page-003.jpg"
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
        doubts: bool = False,
        request_detection: str = "observer",
    ) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            # No page transcription: a restored page is never sent to Claude here.
            sources=SourcesSettings(transcription_enabled=False),
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(
                    enabled=False,
                    request_detection=request_detection,  # type: ignore[arg-type]
                ),
                llm=LlmSettings(),
                editor=EditorSettings(doubts_in_chat=doubts, incorporate_batch_size=1),
            ),
        )

    return make


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _client(make_app(fake)) as client:
        yield client


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


def _base(topic: GenerateTopic | ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


def _start(client: TestClient, topic: GenerateTopic | ReviseTopic) -> str:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["session_id"])


def _settle(client: TestClient) -> None:
    app: Any = client.app
    consumer: AssistantRequestConsumer = app.state.assistant_requests
    chat: DoubtChat = app.state.doubt_chat
    client.portal.call(consumer.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]
    client.portal.call(chat.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]


def _subscribe(client: TestClient, topic: GenerateTopic | ReviseTopic) -> WorkspaceSubscription:
    app: Any = client.app
    return app.state.workspace.subscribe(topic.subject, topic.topic)  # type: ignore[no-any-return]


def _names(events: list[WorkspaceEvent]) -> list[str]:
    return [event.event for event in events if event.event != "reply.delta"]


def _replies(events: list[WorkspaceEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.event == "reply.delta")


def _publish(client: TestClient, session_id: str, payload: dict[str, Any]) -> None:
    bus = client.app.state.bus  # type: ignore[attr-defined]

    async def publish() -> None:
        await bus.publish(session_id, ASSISTANT_REQUEST_KIND, "observer", payload)

    client.portal.call(publish)  # type: ignore[union-attr]


def _spoken(n: int, kind: str, text: str, **fields: Any) -> dict[str, Any]:
    return {
        "request_id": f"req-{n}",
        "kind": kind,
        "summary": text[:140],
        "text": text,
        "segment_ids": [f"seg-{n}"],
        "t_start_ms": 1_000 * n,
        "t_end_ms": 1_000 * n + 800,
        "detector": "observer",
        **fields,
    }


def _classify(fake: FakeClaude, *requests: dict[str, Any]) -> None:
    fake.reply_tool(
        TOOL_NAME,
        {"requests": [{"segment_ids": ["m1"], **request} for request in requests]},
    )


def _incorporation(fake: FakeClaude, n: int) -> None:
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": f"Incorporo la página {n}",
            "ops": [
                {
                    "op": "add_section",
                    "after": "",
                    "level": 2,
                    "title": f"Página {n}",
                    "anchor": f"pagina-{n}",
                    "text": f"Lo de la página {n}.[^p{n}]",
                }
            ],
            "footnotes": [{"label": f"p{n}", "definition": PAGE.format(n=n)}],
        },
        text=f"Incorporo la página {n}.",
    )


def _events(topic: GenerateTopic | ReviseTopic, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (session_id, event.origin, event.payload)
        for session_id, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if event.kind == kind
    ]


def _third_page(topic: GenerateTopic | ReviseTopic, *, aside: bool) -> None:
    meta = {"triage": {"status": "set_aside", "reasons": ["blurry"]}} if aside else {}
    put_source(topic.vault, topic.subject, topic.topic, "notes", "page.jpg", b"\xff\xd8 3", meta)


# -- typed messages ------------------------------------------------------------------------------


def test_a_typed_message_is_classified_with_the_sources_and_incorporates_its_targets(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _classify(
        fake,
        {
            "kind": "incorporate",
            "summary": "Incorporar las dos últimas páginas",
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
        f"{_base(topic)}/workspace/messages", json={"text": "incorpora las dos últimas"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message_id"].startswith("msg-") and body["classified"] is True
    [request] = body["requests"]
    assert request["request_id"] == "req-t1" and request["detector"] == "typed"
    assert request["kind"] == "incorporate" and request["targets"] == [PAGE_2, PAGE_1]
    assert request["text"] == "incorpora las dos últimas"
    _settle(client)

    classify = fake.requests[0]
    assert classify.role == "observer"
    assert classify.tool_choice == {"type": "auto"}
    text = classify.messages[0]["content"][0]["text"]
    assert f"{PAGE_1}: página 1 (apuntes) -- pendiente" in text
    assert f"{PAGE_2}: página 2 (apuntes) -- pendiente" in text
    assert "Doubt asked in the chat now: none." in text
    assert "m1 incorpora las dos últimas" in text

    # Persisted in the live session with origin `user`, then run as a typed incorporation.
    [(sid, origin, payload)] = _events(topic, ASSISTANT_REQUEST_KIND)
    assert (sid, origin) == (session_id, "user")
    assert payload["detector"] == "typed" and payload["message_id"] == body["message_id"]
    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result", "notes.changed"]
    detected, started = events[0].data, events[1].data
    assert detected["origin"] == "typed" and detected["targets"] == [PAGE_2, PAGE_1]
    assert started["origin"] == "typed" and started["kind"] == "incorporate"
    result = next(e.data for e in events if e.event == "turn.result")
    assert result["applied"] is True and sorted(result["source_ids"]) == [PAGE_1, PAGE_2]
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None and "{#definicion}" in notes


def test_typed_messages_without_a_session_go_to_a_review_session(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    _classify(fake, {"kind": "edit", "summary": "Añadir un ejemplo", "targets": [PAGE_1]})
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="De acuerdo.")

    response = client.post(f"{_base(topic)}/workspace/messages", json={"text": "añade un ejemplo"})
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert "targets" not in request or request["targets"] == []  # only for its kinds
    _settle(client)

    [(sid, origin, payload)] = _events(topic, ASSISTANT_REQUEST_KIND)
    meta = {m.id: m for m in list_sessions(topic.vault, topic.subject, topic.topic)}
    assert meta[sid].kind == "review" and meta[sid].ended_at is not None
    assert origin == "user" and payload["kind"] == "edit"
    turns = client.get(f"{_base(topic)}/notes/chat").json()["turns"]
    assert turns[-1]["origin"] == "typed" and turns[-1]["message"] == "añade un ejemplo"


def test_a_classifier_failure_keeps_the_typed_text_as_a_request(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    fake.fail(LLMAPIError("boom"))
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Es por la regla.")

    response = client.post(
        f"{_base(topic)}/workspace/messages", json={"text": "¿por qué es así la derivada?"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["classified"] is False
    [request] = body["requests"]
    assert request["kind"] == "question"
    assert request["summary"] == request["text"] == "¿por qué es así la derivada?"
    _settle(client)
    assert fake.pending == 0  # the editor answered the kept text


def test_an_empty_classification_is_kept_as_an_edit(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    _classify(fake)
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Vale.")

    response = client.post(f"{_base(topic)}/workspace/messages", json={"text": "ponlo en negrita"})
    [request] = response.json()["requests"]
    assert request["kind"] == "edit" and response.json()["classified"] is True
    _settle(client)


def test_typed_messages_are_classified_whatever_the_speech_detection(
    make_app: AppFactory, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    with _client(make_app(fake, request_detection="off")) as client:
        _classify(fake, {"kind": "question", "summary": "¿Qué es la derivada?"})
        fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="Es un límite.")
        response = client.post(
            f"{_base(topic)}/workspace/messages", json={"text": "¿qué es la derivada?"}
        )
        assert response.status_code == 202, response.text
        assert response.json()["requests"][0]["kind"] == "question"
        _settle(client)
        assert fake.requests[0].role == "observer"


def test_typed_message_errors(
    make_app: AppFactory, client: TestClient, topic: GenerateTopic
) -> None:
    assert client.post(f"{_base(topic)}/workspace/messages", json={"text": "  "}).status_code == 422
    assert client.post(f"{_base(topic)}/workspace/messages", json={}).status_code == 422
    unknown = f"/api/subjects/{topic.subject}/topics/no-existe/workspace/messages"
    assert client.post(unknown, json={"text": "hola"}).status_code == 404
    with _client(make_app(None)) as offline:
        refused = offline.post(f"{_base(topic)}/workspace/messages", json={"text": "hola"})
        assert refused.status_code == 503


# -- set aside and restore -----------------------------------------------------------------------


def test_a_spoken_set_aside_writes_the_triage_and_a_chat_entry(
    client: TestClient, topic: GenerateTopic
) -> None:
    _third_page(topic, aside=False)
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)

    _publish(client, session_id, _spoken(1, "set_aside", "aparta la 3", targets=[PAGE_3]))
    _settle(client)

    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result"]  # no notes
    assert events[1].data["kind"] == "set_aside" and events[1].data["origin"] == "voice"
    assert _replies(events) == "He apartado la página 3."
    result = events[-1].data
    assert result["decision"] == "set_aside" and result["source_ids"] == [PAGE_3]
    assert triage_status(topic.vault, topic.subject, topic.topic)[PAGE_3].set_aside
    [(sid, origin, payload)] = _events(topic, CAPTURE_TRIAGED_KIND)
    assert (sid, origin) == (session_id, "user")
    assert payload["status"] == "set_aside" and payload["decided_by"] == "student"

    turns = client.get(f"{_base(topic)}/notes/chat").json()["turns"]
    assert turns[-1]["kind"] == "triage" and turns[-1]["reply"] == "He apartado la página 3."
    assert turns[-1]["origin"] == "voice" and turns[-1]["request_summary"] == "aparta la 3"
    status = client.get(f"{_base(topic)}/sources/status").json()["sources"]
    assert [row["state"] for row in status if row["source_id"] == PAGE_3] == ["apartada"]


def test_a_restore_without_a_session_says_the_page_will_be_transcribed(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    _third_page(topic, aside=True)
    subscription = _subscribe(client, topic)
    _classify(fake, {"kind": "restore", "summary": "Recuperar la página 3", "targets": [PAGE_3]})

    response = client.post(f"{_base(topic)}/workspace/messages", json={"text": "recupera la 3"})
    assert response.status_code == 202, response.text
    _settle(client)

    events = subscription.drain()
    assert _replies(events) == (
        "He recuperado la página 3; se transcribirá en la próxima sesión del tema."
    )
    assert not triage_status(topic.vault, topic.subject, topic.topic)[PAGE_3].set_aside
    [(sid, origin, payload)] = _events(topic, CAPTURE_TRIAGED_KIND)
    assert origin == "user" and payload["status"] == "kept"
    meta = {m.id: m for m in list_sessions(topic.vault, topic.subject, topic.topic)}
    assert meta[sid].kind == "review"


def test_a_restore_of_a_transcribed_page_and_one_not_set_aside(
    client: TestClient, topic: GenerateTopic
) -> None:
    _third_page(topic, aside=True)
    (
        sources_directory(topic.vault, topic.subject, topic.topic, "notes") / "page-003.md"
    ).write_text("Página 3.", encoding="utf-8")
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)

    _publish(
        client, session_id, _spoken(1, "restore", "recupera la 3 y la 1", targets=[PAGE_3, PAGE_1])
    )
    _settle(client)

    assert _replies(subscription.drain()) == (
        "He recuperado la página 3. La página 1 no estaba apartada."
    )
    assert len(_events(topic, CAPTURE_TRIAGED_KIND)) == 1


def test_a_set_aside_of_an_unknown_page_is_a_turn_error(
    client: TestClient, topic: GenerateTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _publish(
        client,
        session_id,
        _spoken(1, "set_aside", "aparta la 9", targets=["sources/notes/page-009.jpg"]),
    )
    _settle(client)
    events = subscription.drain()
    error = events[-1]
    assert error.event == "turn.error" and error.data["status"] == 404


# -- incorporate from speech ---------------------------------------------------------------------


def test_a_spoken_incorporation_is_a_voice_incorporate_turn(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _incorporation(fake, 1)

    _publish(client, session_id, _spoken(1, "incorporate", "incorpora la 1", targets=[PAGE_1]))
    _settle(client)

    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.result", "notes.changed"]
    assert events[1].data["kind"] == "incorporate" and events[1].data["origin"] == "voice"
    turns = client.get(f"{_base(topic)}/notes/chat").json()["turns"]
    assert turns[-1]["kind"] == "incorporate" and turns[-1]["origin"] == "voice"
    assert turns[-1]["request_summary"] == "incorpora la 1"


def test_an_incorporation_of_a_set_aside_page_is_a_turn_error(
    client: TestClient, topic: GenerateTopic
) -> None:
    _third_page(topic, aside=True)
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _publish(client, session_id, _spoken(1, "incorporate", "incorpora la 3", targets=[PAGE_3]))
    _settle(client)
    error = subscription.drain()[-1]
    assert error.event == "turn.error" and error.data["status"] == 422
    assert "apartada" in error.data["detail"]


# -- doubt answers -------------------------------------------------------------------------------


QUESTION = "¿Qué pone después de «cociente»?"


def _ask_a_doubt(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> str:
    """A typed turn that raises a doubt, the review of p-1, then the doubt asked in the chat."""
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Aclaro la definición",
            "ops": [
                {
                    "op": "replace_block",
                    "section": "definicion",
                    "block": 1,
                    "text": "**Derivada**: el límite del cociente.[^p1][^t1]",
                }
            ],
            "doubts": [
                {
                    "kind": "illegible",
                    "text": "Palabra dudosa tras «cociente» en la página 1.",
                    "question": QUESTION,
                    "suggestions": ["incremental", "diferencial"],
                    "refs": [PAGE_1],
                }
            ],
        },
        text="Lo aclaro y te pregunto lo que no se lee.",
    )
    fake.reply_tool(
        REVIEW_TOOL,
        {
            "decisions": [
                {
                    "pending_id": "p-1",
                    "action": "auto_resolve",
                    "resolution": "La regla de la cadena se repasa el próximo día.",
                    "evidence": [
                        {
                            "source_id": f"sessions/{topic.session}#t=01:02:05-01:02:10",
                            "quote": "Mañana repasamos la regla de la cadena.",
                        }
                    ],
                }
            ]
        },
    )
    subscription = _subscribe(client, topic)
    response = client.post(f"{_base(topic)}/notes/chat", json={"message": "Aclara la definición"})
    assert response.status_code == 200, response.text
    _settle(client)
    asked = [e.data for e in subscription.drain() if e.event == "doubt.asked"]
    subscription.close()
    assert [a["question"] for a in asked] == [QUESTION]
    return str(asked[0]["pending_id"])


def test_a_typed_answer_to_the_asked_doubt_resolves_it(
    make_app: AppFactory, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    with _client(make_app(fake, doubts=True)) as client:
        pending_id = _ask_a_doubt(client, fake, topic)
        subscription = _subscribe(client, topic)
        _classify(
            fake,
            {
                "kind": "doubt_answer",
                "summary": "Responder: la primera",
                "pending_id": pending_id,
                "answer": "1",
            },
        )
        fake.reply_tool(
            DECISION_TOOL,
            {
                "resolution": "Pone «incremental».",
                "edits": [
                    {
                        "op": "replace_block",
                        "section": "definicion",
                        "block": 1,
                        "text": "**Derivada**: el límite del cociente incremental.[^p1][^t1]",
                    }
                ],
            },
        )

        response = client.post(f"{_base(topic)}/workspace/messages", json={"text": "la primera"})
        assert response.status_code == 202, response.text
        [request] = response.json()["requests"]
        assert request["kind"] == "doubt_answer"
        assert (request["pending_id"], request["answer"]) == (pending_id, "1")
        _settle(client)

        text = fake.requests[-2].messages[0]["content"][0]["text"]
        assert f"Doubt asked in the chat now: {pending_id} «{QUESTION}»" in text
        assert "suggestion 1: incremental" in text
        events = subscription.drain()
        assert _names(events) == [
            "request.detected",
            "turn.started",
            "doubt.resolved",
            "notes.changed",
            "turn.result",
        ]
        assert events[1].data["kind"] == "doubt_answer"
        assert _replies(events) == "Pone «incremental»."
        resolved = next(e.data for e in events if e.event == "doubt.resolved")
        assert resolved["pending_id"] == pending_id and resolved["status"] == "resolved"
        notes = read_notes(topic.vault, topic.subject, topic.topic)
        assert notes is not None and "cociente incremental" in notes
        turns = client.get(f"{_base(topic)}/notes/chat").json()["turns"]
        doubt = next(t for t in turns if t["kind"] == "doubt")
        assert doubt["status"] == "resolved" and doubt["answer"] == "incremental"


def test_a_doubt_answer_is_refused_without_an_asked_doubt(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_revise_topic(tmp_vault)
    _classify(
        fake, {"kind": "doubt_answer", "summary": "La segunda", "pending_id": "p-9", "answer": "2"}
    )
    _classify(fake, {"kind": "question", "summary": "¿La segunda qué?"})
    fake.reply_tool(EDIT_TOOL, {"summary": "Nada", "ops": []}, text="¿La segunda qué?")

    response = client.post(f"{_base(topic)}/workspace/messages", json={"text": "la segunda"})
    [request] = response.json()["requests"]
    assert request["kind"] == "question"
    reask = fake.requests[1].messages[-1]["content"]
    assert any("no doubt is asked" in str(block) for block in reask)
    _settle(client)
