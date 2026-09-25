"""`POST /api/subjects/{s}/topics/{t}/notes/generate`: "prepárame el tema" over REST."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic, valid_notes
from studentassistant.config import LlmSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude, LLMServerError
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, read_jsonl, read_notes, sessions_directory
from studentassistant.vault.session_models import Event

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


def _route(topic: GenerateTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate"


AppFactory = Callable[..., FastAPI]


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
            # The observer stays off, so every scripted reply is the editor's.
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


def test_generates_tags_and_answers_the_result(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    fake.reply_text(valid_notes(topic.session))

    response = client.post(_route(topic))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is False and body["version"] == 1
    assert body["tag"] == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert body["model"] == "claude-opus-5-5"
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    assert fake.requests[0].role == "editor"
    # The topic summary now shows the version.
    summary = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/summary")
    assert summary.json()["notes_version"] == 1


def test_without_a_transport_the_route_is_unavailable(
    make_app: AppFactory, topic: GenerateTopic
) -> None:
    client = TestClient(make_app(None), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))
    response = client.post(_route(topic))
    assert response.status_code == 503
    assert "no está disponible" in response.json()["detail"]


def test_an_unknown_topic_is_404(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    response = client.post(f"/api/subjects/{topic.subject}/topics/no-existe/notes/generate")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."
    assert fake.requests == []


def test_a_reached_cap_is_409_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: GenerateTopic
) -> None:
    app = make_app(fake, LlmSettings(max_usd_per_day=0))
    fake.reply_text(valid_notes(topic.session))
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        refused = client.post(_route(topic))
        assert refused.status_code == 409
        assert "Confirma" in refused.json()["detail"]
        assert refused.json()["code"] == "cost_cap_reached"
        assert fake.requests == []

        confirmed = client.post(_route(topic), json={"confirm_over_cap": True})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["version"] == 1


def test_a_claude_failure_is_502_and_writes_nothing(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))
    response = client.post(_route(topic))
    assert response.status_code == 502
    assert "No se han podido generar los apuntes" in response.json()["detail"]
    assert read_notes(topic.vault, topic.subject, topic.topic) is None


def test_the_event_is_published_to_the_topics_active_session(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    fake.reply_text(valid_notes(topic.session))

    response = client.post(_route(topic))

    assert response.status_code == 200, response.text
    path = sessions_directory(topic.vault, topic.subject, topic.topic) / session_id
    events: list[Any] = list(read_jsonl(path / "events.jsonl", Event))
    [generated] = [e for e in events if e.kind == "notes.generated"]
    assert generated.origin == "editor"
    assert generated.payload["version"] == 1 and generated.payload["draft"] is False
