"""Generation requests in the study chat: `POST .../tutor` `style: "written"` (#366)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quiz_replies import reply_quiz
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import LlmSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.editor.tutor import ANSWER_RECORD, CONVERSATION_NAME, TutorAnswer
from studentassistant.generators.slides import PDF_NAME, PPTX_NAME, Exported, SlidesGenerator
from studentassistant.generators.slides import TOOL_NAME as SLIDES_TOOL
from studentassistant.llm import FakeClaude, LLMServerError
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    create_topic,
    write_notes,
)

LOCAL_BASE_URL = "http://localhost:8765"
AppFactory = Callable[..., FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude | None, llm: LlmSettings | None = None) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False), llm=llm or LlmSettings()
            ),
        )

    return make


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _local(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _local(make_app(fake)) as client:
        yield client


def _topic_base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


def events_of(response: Any) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in response.text.split("\n\n"):
        if not chunk.strip():
            continue
        fields = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def _ask(client: TestClient, topic: ReviseTopic, question: str, **extra: Any) -> Any:
    return client.post(
        f"{_topic_base(topic)}/tutor", json={"question": question, "style": "written", **extra}
    )


def _option(study: dict[str, Any], key: str) -> str:
    return next(option["state"] for option in study["options"] if option["key"] == key)


def test_a_request_generates_the_material_and_streams_it(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    reply_quiz(fake)
    response = _ask(client, topic, "Hazme un quiz de 3 preguntas")

    assert response.status_code == 200, response.text
    events = events_of(response)
    assert [kind for kind, _ in events] == ["generation.started", "result"]
    started = events[0][1]
    assert started["kind"] == "quiz" and started["option"] == "quiz"
    assert started["text"].startswith("Preparando un quiz de 3 preguntas")
    result = events[1][1]
    assert result["kind"] == "generation" and result["option"] == "quiz"
    assert result["material_kind"] == "quiz" and result["items"] == 3
    assert result["reply"] == "Listo: 3 preguntas. Ábrelo en «Quiz»."
    assert isinstance(result["warnings"], list)
    assert _option(result["study"], "quiz") == "listo"
    assert _option(result["study"], "esquema") == "sin_generar"
    # The generator ran, not the tutor.
    assert [request.role for request in fake.requests] == ["generator"]
    assert client.get(f"{_topic_base(topic)}/study").json() == result["study"]

    [turn] = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert turn["kind"] == "generation" and turn["style"] == "written"
    assert turn["question"] == "Hazme un quiz de 3 preguntas"
    assert turn["reply"] == result["reply"]
    assert turn["option"] == "quiz" and turn["items"] == 3


def test_a_stale_material_is_regenerated(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    reply_quiz(fake)
    assert events_of(_ask(client, topic, "hazme un quiz de 3 preguntas"))[-1][0] == "result"
    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nOtro.[^p1]\n")
    GitSync(topic.vault).checkpoint("edit")
    assert _option(client.get(f"{_topic_base(topic)}/study").json(), "quiz") == "desactualizado"

    reply_quiz(fake)
    kind, result = events_of(_ask(client, topic, "hazme de nuevo el quiz de 3"))[-1]
    assert kind == "result" and _option(result["study"], "quiz") == "listo"
    assert len(client.get(f"{_topic_base(topic)}/tutor").json()["turns"]) == 2


class _FakeExporter:
    async def export(self, markdown: str, assets: dict[str, bytes]) -> Exported:
        return Exported(files={PDF_NAME: b"%PDF-1.7 fake", PPTX_NAME: b"PK fake pptx"})


SLIDES_DECK: dict[str, Any] = {
    "title": "Derivadas",
    "slides": [
        {
            "title": "Qué es la derivada",
            "bullets": ["**Derivada**: el límite del cociente incremental."],
            "anchors": ["definicion"],
        },
        {"title": "Próximo día", "bullets": ["La regla de la cadena."], "anchors": ["proximo-dia"]},
    ],
}


def test_slides_are_the_sixth_study_option(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SlidesGenerator, "exporter", _FakeExporter())
    before = client.get(f"{_topic_base(topic)}/study").json()
    assert [option["key"] for option in before["options"]] == [
        "esquema",
        "ejercicios",
        "examen",
        "quiz",
        "tarjetas",
        "diapositivas",
    ]
    assert _option(before, "diapositivas") == "sin_generar"

    fake.reply_tool(SLIDES_TOOL, SLIDES_DECK)
    kind, result = events_of(_ask(client, topic, "hazme 2 diapositivas"))[-1]
    assert kind == "result", result
    assert result["option"] == "diapositivas" and result["material_kind"] == "diapositivas"
    items = result["items"]
    assert result["reply"] == (
        f"Listas: {items} diapositiva{'' if items == 1 else 's'}. Ábrelas en «Diapositivas»."
    )
    assert _option(result["study"], "diapositivas") == "listo"
    assert client.get(f"{_topic_base(topic)}/study").json() == result["study"]
    # The downloads come from the materials listing, as on the topic card page.
    materials = client.get(f"{_topic_base(topic)}/generated").json()
    [slides] = [a for a in materials["artifacts"] if a["kind"] == "diapositivas"]
    names = {Path(name).name for name in slides["files"]}
    assert {"diapositivas.md", PDF_NAME, PPTX_NAME} <= names
    pdf = client.get(f"{_topic_base(topic)}/generated/files/{PDF_NAME}")
    assert pdf.status_code == 200 and pdf.content == b"%PDF-1.7 fake"

    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nOtro.[^p1]\n")
    GitSync(topic.vault).checkpoint("edit")
    stale = next(
        option
        for option in client.get(f"{_topic_base(topic)}/study").json()["options"]
        if option["key"] == "diapositivas"
    )
    assert stale["state"] == "desactualizado" and stale["stale_reason"]


def test_a_reached_cap_is_a_coded_error_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    with _local(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        events = events_of(_ask(client, topic, "hazme un quiz de 3 preguntas"))
        assert [kind for kind, _ in events] == ["generation.started", "error"]
        error = events[1][1]
        assert error["status"] == 409 and error["code"] == "cost_cap_reached"
        assert "Confirma para generar" in error["detail"]
        assert fake.requests == []
        assert client.get(f"{_topic_base(topic)}/tutor").json()["turns"] == []

        reply_quiz(fake)
        confirmed = _ask(client, topic, "hazme un quiz de 3 preguntas", confirm_over_cap=True)
        assert events_of(confirmed)[-1][0] == "result"


def test_the_same_material_generating_is_busy(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    materials = client.app.state.materials  # type: ignore[attr-defined]
    assert materials.claim(topic.subject, topic.topic, "quiz")
    kind, error = events_of(_ask(client, topic, "hazme un quiz de 3 preguntas"))[-1]
    assert kind == "error" and error["status"] == 409 and "Ya se está generando" in error["detail"]
    materials.release(topic.subject, topic.topic, "quiz")
    assert fake.requests == []


def test_no_notes_and_claude_failures(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    bare = create_topic(topic.vault, topic.subject, "Integrales").slug
    response = client.post(
        f"/api/subjects/{topic.subject}/topics/{bare}/tutor",
        json={"question": "hazme un quiz", "style": "written"},
    )
    kind, error = events_of(response)[-1]
    assert kind == "error" and error["status"] == 409 and "apuntes" in error["detail"]

    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))
    kind, error = events_of(_ask(client, topic, "hazme un quiz de 3 preguntas"))[-1]
    assert kind == "error" and error["status"] == 502
    # A failed generation is not saved.
    assert client.get(f"{_topic_base(topic)}/tutor").json()["turns"] == []


def test_non_matches_and_spoken_questions_still_go_to_the_tutor(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    fake.reply_text("Un quiz son preguntas cortas.")
    events = events_of(_ask(client, topic, "¿Qué es un quiz?"))
    assert events[-1][0] == "result" and events[-1][1]["reply"].startswith("Un quiz")
    fake.reply_text("Vale.")
    spoken = client.post(f"{_topic_base(topic)}/tutor", json={"question": "hazme un quiz"})
    assert events_of(spoken)[-1][1]["style"] == "spoken"
    assert [request.role for request in fake.requests] == ["editor", "editor"]
    turns = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert [turn["kind"] for turn in turns] == ["answer", "answer"]
    assert all(turn["option"] is None and turn["items"] is None for turn in turns)


def test_older_answer_records_still_read(client: TestClient, topic: ReviseTopic) -> None:
    from datetime import UTC, datetime

    answer = TutorAnswer(subject=topic.subject, topic=topic.topic, question="¿Qué?", reply="Eso.")
    detail = answer.model_dump(mode="json")
    detail.pop("style")
    detail.pop("sections")
    append_conversation_record(
        topic.vault,
        topic.subject,
        topic.topic,
        CONVERSATION_NAME,
        ConversationRecord(time=datetime.now(UTC), kind=ANSWER_RECORD, detail=detail),
    )
    [turn] = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert turn["kind"] == "answer" and turn["style"] == "spoken" and turn["reply"] == "Eso."


# -- asking back (#383) ----------------------------------------------------------------------------

QUIZ_QUESTION = (
    "¿Cuántas preguntas quieres y de qué dificultad (fácil, media, difícil o variada)? Si no me"
    " dices nada distinto, hago 10 preguntas de dificultad variada."
)


def test_a_bare_request_asks_back_and_generates_nothing(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    events = events_of(_ask(client, topic, "Hazme un quiz"))
    assert events == [
        (
            "result",
            {
                "kind": "clarification",
                "option": "quiz",
                "style": "written",
                "question": "Hazme un quiz",
                "reply": QUIZ_QUESTION,
                "refs": [],
                "sections": [],
                "warning": None,
                "defaults": {"size": 10, "difficulty": "mixed"},
            },
        )
    ]
    assert fake.requests == []
    [turn] = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert turn["kind"] == "clarification" and turn["option"] == "quiz"
    assert turn["style"] == "written" and turn["reply"] == QUIZ_QUESTION
    assert turn["items"] is None
    assert _option(client.get(f"{_topic_base(topic)}/study").json(), "quiz") == "sin_generar"


def test_the_follow_up_completes_the_request(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    _ask(client, topic, "hazme un quiz")
    reply_quiz(fake)
    events = events_of(_ask(client, topic, "5 difíciles"))
    assert [kind for kind, _ in events] == ["generation.started", "result"]
    assert events[0][1]["text"].startswith("Preparando un quiz de 5 preguntas difíciles")
    assert events[1][1]["kind"] == "generation" and events[1][1]["option"] == "quiz"
    assert [request.role for request in fake.requests] == ["generator"]
    turns = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert [turn["kind"] for turn in turns] == ["clarification", "generation"]
    assert turns[1]["question"] == "5 difíciles"

    # Answered: a later count is no completion.
    fake.reply_text("¿Diez qué?")
    later = events_of(_ask(client, topic, "10"))
    assert later[-1][1]["reply"] == "¿Diez qué?"
    assert [request.role for request in fake.requests] == ["generator", "editor"]


def test_an_acceptance_generates_with_the_defaults(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    _ask(client, topic, "dame un test")
    reply_quiz(fake)
    events = events_of(_ask(client, topic, "Vale"))
    assert events[0][0] == "generation.started"
    assert events[0][1]["text"].startswith(
        "Preparando un quiz de 10 preguntas de dificultad variada"
    )
    assert events[-1][0] == "result" and events[-1][1]["kind"] == "generation"


def test_a_count_only_option_asks_for_its_count(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    [(kind, result)] = events_of(_ask(client, topic, "hazme tarjetas"))
    assert kind == "result" and result["kind"] == "clarification"
    assert result["option"] == "tarjetas" and result["defaults"] == {"size": 20}
    assert result["reply"] == "¿Cuántas tarjetas quieres? Por defecto, 20."
    # A new bare request replaces the pending one.
    [(_, again)] = events_of(_ask(client, topic, "hazme un quiz"))
    assert again["kind"] == "clarification" and again["option"] == "quiz"
    assert fake.requests == []


def test_a_question_after_the_clarification_drops_it(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    _ask(client, topic, "hazme un quiz")
    fake.reply_text("Es el límite del cociente incremental.")
    answer = events_of(_ask(client, topic, "¿Qué es la derivada?"))[-1][1]
    assert answer["reply"].startswith("Es el límite")
    fake.reply_text("¿Diez qué?")
    later = events_of(_ask(client, topic, "10"))[-1][1]
    assert later["reply"] == "¿Diez qué?"
    assert [request.role for request in fake.requests] == ["editor", "editor"]
    turns = client.get(f"{_topic_base(topic)}/tutor").json()["turns"]
    assert [turn["kind"] for turn in turns] == ["clarification", "answer", "answer"]


def test_a_request_with_its_parameters_generates_directly(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    reply_quiz(fake)
    events = events_of(_ask(client, topic, "hazme un quiz de 10 preguntas fáciles"))
    assert [kind for kind, _ in events] == ["generation.started", "result"]
    assert events[0][1]["text"].startswith("Preparando un quiz de 10 preguntas fáciles")
