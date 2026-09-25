"""The voice tutor API: `POST/GET /api/subjects/{s}/topics/{t}/tutor` (SSE)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import LlmSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude, LLMServerError
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_topic, read_notes

LOCAL_BASE_URL = "http://localhost:8765"
AppFactory = Callable[..., FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/tutor"


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


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with TestClient(make_app(fake), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def events_of(response: Any) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in response.text.split("\n\n"):
        if not chunk.strip():
            continue
        fields = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_a_question_streams_the_answer_with_its_refs(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    fake.reply_text("Es el límite del cociente incremental.[^p1]")
    response = client.post(_base(topic), json={"question": "¿Qué es la derivada?"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = events_of(response)
    kinds = [kind for kind, _ in events]
    assert set(kinds[:-1]) == {"reply.delta"} and kinds[-1] == "result"
    result = events[-1][1]
    assert result["question"] == "¿Qué es la derivada?"
    assert result["reply"] == "Es el límite del cociente incremental.[^p1]"
    assert [(r["label"], r["kind"], r["text"]) for r in result["refs"]] == [
        ("p1", "notes", "Apuntes, página 1")
    ]
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes

    history = client.get(_base(topic)).json()
    assert [turn["question"] for turn in history["turns"]] == ["¿Qué es la derivada?"]
    assert history["turns"][0]["refs"][0]["label"] == "p1"
    # The editor chat is not concerned, and the notes lock was never taken.
    chat = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/chat").json()
    assert chat["turns"] == []


def test_the_tutor_answers_while_the_notes_are_busy(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    generator = client.app.state.notes  # type: ignore[attr-defined]
    assert generator.claim(topic.subject, topic.topic)
    fake.reply_text("Sí.")
    events = events_of(client.post(_base(topic), json={"question": "¿Seguro?"}))
    assert events[-1][0] == "result"
    generator.release(topic.subject, topic.topic)


def test_failures_in_the_stream(
    make_app: AppFactory, client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))
    ((kind, data),) = events_of(client.post(_base(topic), json={"question": "¿Qué?"}))
    assert kind == "error" and data["status"] == 502 and "Claude" in data["detail"]
    # The topic's lock was released.
    fake.reply_text("Vale.")
    assert events_of(client.post(_base(topic), json={"question": "¿Qué?"}))[-1][0] == "result"

    refusing = FakeClaude().reply_text("No.", stop_reason="refusal")
    with TestClient(
        make_app(refusing), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)
    ) as other:
        kind, data = events_of(other.post(_base(topic), json={"question": "¿Qué?"}))[-1]
        assert kind == "error" and data["status"] == 502 and "negado" in data["detail"]


def test_a_reached_cap_is_a_coded_error_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    app = make_app(fake, LlmSettings(max_usd_per_day=0))
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        ((kind, data),) = events_of(client.post(_base(topic), json={"question": "¿Qué?"}))
        assert kind == "error" and data["status"] == 409
        assert data["code"] == "cost_cap_reached" and "Confirma" in data["detail"]
        assert fake.requests == []

        fake.reply_text("Vale.")
        confirmed = client.post(_base(topic), json={"question": "¿Qué?", "confirm_over_cap": True})
        assert events_of(confirmed)[-1][0] == "result"


def test_errors_before_the_stream(
    client: TestClient, make_app: AppFactory, tmp_vault: Vault, topic: ReviseTopic
) -> None:
    assert client.post(_base(topic), json={"question": ""}).status_code == 422
    blank = client.post(_base(topic), json={"question": "   "})
    assert blank.status_code == 422 and "preguntar" in blank.json()["detail"]
    assert client.post(_base(topic), json={"question": "x" * 1001}).status_code == 422
    missing = client.post("/api/subjects/nada/topics/nada/tutor", json={"question": "Hola"})
    assert missing.status_code == 404
    assert client.get("/api/subjects/nada/topics/nada/tutor").status_code == 404
    bare = create_topic(tmp_vault, topic.subject, "Integrales").slug
    no_notes = client.post(
        f"/api/subjects/{topic.subject}/topics/{bare}/tutor", json={"question": "Hola"}
    )
    assert no_notes.status_code == 409 and "prepáralos" in no_notes.json()["detail"]

    no_claude = TestClient(make_app(None), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))
    assert no_claude.post(_base(topic), json={"question": "Hola"}).status_code == 503
    assert no_claude.get(_base(topic)).json()["turns"] == []
