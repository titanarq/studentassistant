"""The editor chat API: `POST/GET .../notes/chat` (SSE) and `POST .../notes/chat/undo`."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revise_topic import BOOK_FOOTNOTE, ReviseTopic, make_revise_topic
from studentassistant.config import LlmSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.editor.revise import EDIT_TOOL
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
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/chat"


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
    """The Server-Sent Events of a response body, as `(event, data)`."""
    events = []
    for chunk in response.text.split("\n\n"):
        if not chunk.strip():
            continue
        fields = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_a_turn_streams_the_reply_then_the_applied_diff_and_can_be_undone(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Añado la explicación del libro",
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 1,
                    "text": "Según el libro, es el límite de (f(a+h) - f(a)) / h.[^b1]",
                }
            ],
            "footnotes": [{"label": "b1", "definition": BOOK_FOOTNOTE}],
        },
        text="Añado la explicación del libro.",
    )

    response = client.post(_base(topic), json={"message": "Usa la explicación del libro"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = events_of(response)
    kinds = [kind for kind, _ in events]
    assert set(kinds[:-1]) == {"reply.delta"} and kinds[-1] == "result"
    assert "".join(data["text"] for kind, data in events if kind == "reply.delta") == (
        "Añado la explicación del libro."
    )
    result = events[-1][1]
    assert result["applied"] and result["changed_sections"] == ["definicion"]
    assert "+Según el libro" in result["diff"] and result["commit"]
    assert "Según el libro" in (read_notes(topic.vault, topic.subject, topic.topic) or "")

    history = client.get(_base(topic)).json()
    assert history["can_undo"] is True
    assert [turn["message"] for turn in history["turns"]] == ["Usa la explicación del libro"]

    undo = client.post(f"{_base(topic)}/undo")
    assert undo.status_code == 200, undo.text
    assert undo.json()["undone_commit"] == result["commit"] and undo.json()["notes_changed"]
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
    again = client.post(f"{_base(topic)}/undo")
    assert again.status_code == 409 and "deshacer" in again.json()["detail"]
    assert client.get(_base(topic)).json()["turns"][0]["undone"] is True


def test_a_claude_failure_is_an_error_event(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))

    response = client.post(_base(topic), json={"message": "Pon un ejemplo"})

    assert response.status_code == 200
    ((kind, data),) = events_of(response)
    assert kind == "error" and data["status"] == 502 and "Claude" in data["detail"]
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
    # The lock was released: the next turn runs.
    fake.reply_text("Vale.")
    assert events_of(client.post(_base(topic), json={"message": "Hola"}))[-1][0] == "result"


def test_a_reached_cap_is_a_coded_error_event_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    app = make_app(fake, LlmSettings(max_usd_per_day=0))
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        ((kind, data),) = events_of(client.post(_base(topic), json={"message": "Pon un ejemplo"}))
        assert kind == "error" and data["status"] == 409
        assert data["code"] == "cost_cap_reached" and "Confirma" in data["detail"]
        assert fake.requests == []

        fake.reply_text("Vale.")
        confirmed = client.post(
            _base(topic), json={"message": "Pon un ejemplo", "confirm_over_cap": True}
        )
        assert events_of(confirmed)[-1][0] == "result"


def test_errors_before_the_stream(
    client: TestClient, make_app: AppFactory, tmp_vault: Vault, topic: ReviseTopic
) -> None:
    assert client.post(_base(topic), json={"message": ""}).status_code == 422
    missing = client.post("/api/subjects/nada/topics/nada/notes/chat", json={"message": "Hola"})
    assert missing.status_code == 404
    bare = create_topic(tmp_vault, topic.subject, "Integrales").slug
    no_notes = client.post(
        f"/api/subjects/{topic.subject}/topics/{bare}/notes/chat", json={"message": "Hola"}
    )
    assert no_notes.status_code == 409 and "prepáralos" in no_notes.json()["detail"]
    assert client.post(f"{_base(topic)}/undo").status_code == 409

    generator = client.app.state.notes  # type: ignore[attr-defined]
    assert generator.claim(topic.subject, topic.topic)
    busy = client.post(_base(topic), json={"message": "Hola"})
    assert busy.status_code == 409 and "ya está trabajando" in busy.json()["detail"]
    generator.release(topic.subject, topic.topic)

    no_claude = TestClient(make_app(None), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))
    assert no_claude.post(_base(topic), json={"message": "Hola"}).status_code == 503
    assert no_claude.get(_base(topic)).status_code == 200
