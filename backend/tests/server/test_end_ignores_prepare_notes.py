"""`POST /api/sessions/{id}/end` ignores the retired `prepare_notes` flag (#440, was #258).

Old clients may still send it: the end is accepted and behaves exactly as without it, nothing is
generated, and the retired `GET .../notes/generation` polling route is gone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic, valid_notes
from studentassistant.config import EditorSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, read_notes

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, fake: FakeClaude
) -> Iterator[TestClient]:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=fake,
        # The observer stays off, so any Claude call would be the editor's.
        llm_settings=Settings(
            observer=ObserverSettings(enabled=False),
            editor=EditorSettings(prepare_mode="single"),
        ),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


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


@pytest.mark.parametrize("extra", [{}, {"prepare_notes": True}, {"prepare_notes": False}])
def test_an_end_is_accepted_with_or_without_the_flag_and_generates_nothing(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic, extra: dict[str, Any]
) -> None:
    # A reply is scripted so a generation, were one started, could run and write the notes.
    fake.reply_text(valid_notes(topic.session))
    session_id = _start(client, topic)

    body = _end(client, session_id, **extra)

    assert set(body) == {"session_id", "status", "ended_at_ms"}
    assert body["session_id"] == session_id and body["status"] == "ended"
    assert fake.requests == []
    assert read_notes(topic.vault, topic.subject, topic.topic) is None
    # The session really ended: ending it again is a clash with its state.
    again = client.post(
        f"/api/sessions/{session_id}/end", json={"client_time_ms": 3_000, "reason": "button"}
    )
    assert again.status_code == 409


def test_the_generation_status_route_is_gone(client: TestClient, topic: GenerateTopic) -> None:
    response = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generation")

    assert response.status_code in (404, 405)
