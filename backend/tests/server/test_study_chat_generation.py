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
    kind, result = events_of(_ask(client, topic, "hazme el quiz de nuevo"))[-1]
    assert kind == "result" and _option(result["study"], "quiz") == "listo"
    assert len(client.get(f"{_topic_base(topic)}/tutor").json()["turns"]) == 2


def test_a_reached_cap_is_a_coded_error_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    with _local(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        events = events_of(_ask(client, topic, "hazme un quiz"))
        assert [kind for kind, _ in events] == ["generation.started", "error"]
        error = events[1][1]
        assert error["status"] == 409 and error["code"] == "cost_cap_reached"
        assert "Confirma para generar" in error["detail"]
        assert fake.requests == []
        assert client.get(f"{_topic_base(topic)}/tutor").json()["turns"] == []

        reply_quiz(fake)
        confirmed = _ask(client, topic, "hazme un quiz", confirm_over_cap=True)
        assert events_of(confirmed)[-1][0] == "result"


def test_the_same_material_generating_is_busy(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    materials = client.app.state.materials  # type: ignore[attr-defined]
    assert materials.claim(topic.subject, topic.topic, "quiz")
    kind, error = events_of(_ask(client, topic, "hazme un quiz"))[-1]
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
    kind, error = events_of(_ask(client, topic, "hazme un quiz"))[-1]
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
