"""`POST /api/sessions/{id}/end` with `prepare_notes` and `GET .../notes/generation` (#258)."""

from __future__ import annotations

import time
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
from studentassistant.vault import Vault, read_notes

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0

AppFactory = Callable[..., FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


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


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _client(make_app(fake)) as client:
        yield client


def _status_route(topic: GenerateTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generation"


def _start(client: TestClient, topic: GenerateTopic) -> str:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    return started.json()["session_id"]


def _end(client: TestClient, session_id: str, **extra: Any) -> dict[str, Any]:
    ended = client.post(
        f"/api/sessions/{session_id}/end",
        json={"client_time_ms": 2_000, "reason": "button", **extra},
    )
    assert ended.status_code == 200, ended.text
    return ended.json()


def _settled(client: TestClient, topic: GenerateTopic) -> dict[str, Any]:
    """The status once it is no longer `running`, polled for at most `WAIT_SECONDS`."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        response = client.get(_status_route(topic))
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "running":
            return body
        assert time.monotonic() < deadline, "the background generation did not finish"
        time.sleep(0.02)


def test_end_without_the_flag_is_unchanged(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    session_id = _start(client, topic)

    body = _end(client, session_id)

    assert set(body) == {"session_id", "status", "ended_at_ms"}
    assert body["status"] == "ended"
    assert client.get(_status_route(topic)).json() == {
        "subject_id": topic.subject,
        "topic_id": topic.topic,
        "status": "idle",
    }
    assert fake.requests == []
    assert read_notes(topic.vault, topic.subject, topic.topic) is None


def test_end_with_the_flag_generates_notes_v1_in_the_background(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    fake.reply_text(valid_notes(topic.session))
    session_id = _start(client, topic)

    body = _end(client, session_id, prepare_notes=True)

    assert body["status"] == "ended"
    assert body["notes_generation"] == "started"
    status = _settled(client, topic)
    assert status["status"] == "done", status
    assert status["version"] == 1 and status["draft"] is False
    assert status["started_at_ms"] <= status["finished_at_ms"]
    assert "detail" not in status
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    assert fake.requests[0].role == "editor"


def test_a_running_generation_is_reported_not_duplicated(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    notes = client.app.state.notes  # type: ignore[attr-defined]
    assert notes.claim(topic.subject, topic.topic)  # a generation of the topic is running
    session_id = _start(client, topic)

    body = _end(client, session_id, prepare_notes=True)

    assert body["notes_generation"] == "running"
    busy = client.post(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate")
    assert busy.status_code == 409
    assert fake.requests == []
    notes.release(topic.subject, topic.topic)


def test_the_background_generation_releases_the_topic_lock(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    notes = client.app.state.notes  # type: ignore[attr-defined]
    fake.reply_text(valid_notes(topic.session))
    session_id = _start(client, topic)

    _end(client, session_id, prepare_notes=True)

    # Once it settles, the topic is free for the next generation.
    _settled(client, topic)
    assert notes.claim(topic.subject, topic.topic)
    notes.release(topic.subject, topic.topic)


def test_a_reached_cost_cap_needs_confirmation(
    make_app: AppFactory, fake: FakeClaude, topic: GenerateTopic
) -> None:
    fake.reply_text(valid_notes(topic.session))
    with _client(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        session_id = _start(client, topic)

        assert _end(client, session_id, prepare_notes=True)["notes_generation"] == "started"
        status = _settled(client, topic)

        assert status["status"] == "needs_confirmation", status
        assert "Confirma" in status["detail"]
        assert fake.requests == []
        assert read_notes(topic.vault, topic.subject, topic.topic) is None

        # The student confirms through the existing route; the status follows it.
        confirmed = client.post(
            f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate",
            json={"confirm_over_cap": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        after = client.get(_status_route(topic)).json()
        assert after["status"] == "done" and after["version"] == 1


def test_a_claude_failure_is_failed_and_leaves_the_notes_untouched(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))
    session_id = _start(client, topic)

    _end(client, session_id, prepare_notes=True)
    status = _settled(client, topic)

    assert status["status"] == "failed", status
    assert "No se han podido generar los apuntes" in status["detail"]
    assert "version" not in status
    assert read_notes(topic.vault, topic.subject, topic.topic) is None


def test_without_claude_the_flag_is_unavailable(make_app: AppFactory, topic: GenerateTopic) -> None:
    with _client(make_app(None)) as client:
        session_id = _start(client, topic)

        assert _end(client, session_id, prepare_notes=True)["notes_generation"] == "unavailable"
        assert client.get(_status_route(topic)).json()["status"] == "idle"


def test_the_status_of_an_unknown_topic_is_404(client: TestClient, topic: GenerateTopic) -> None:
    response = client.get(f"/api/subjects/{topic.subject}/topics/no-existe/notes/generation")
    assert response.status_code == 404
