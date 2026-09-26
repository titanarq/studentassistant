"""Spoken requests become editor turns (`server/assistant_requests.py`) and the workspace hub feed.

Every test runs the real app (`FakeClaude`, `tmp_vault`, the FastAPI test client): an
`assistant.request` is published on the bus of an active session, as the observer's detector does,
and the turn is read back from `GET .../notes/chat` and from a workspace hub subscription.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import valid_notes
from revise_topic import BOOK_FOOTNOTE, ReviseTopic, make_revise_topic
from studentassistant.config import (
    EditorSettings,
    LlmSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
)
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude
from studentassistant.observer import ASSISTANT_REQUEST_KIND
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.vault import Vault, read_notes

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
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
            # The observer stays off, so every scripted reply is the editor's; so does the
            # doubts chat (#325, `test_doubt_chat.py`), which would review the topic's doubts.
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False),
                llm=llm or LlmSettings(),
                editor=EditorSettings(doubts_in_chat=False),
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


def _chat(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/chat"


def _start(client: TestClient, topic: ReviseTopic) -> str:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["session_id"])


def _request(
    n: int, kind: str = "edit", text: str = "pon aquí la explicación del libro"
) -> dict[str, Any]:
    return {
        "request_id": f"req-{n}",
        "kind": kind,
        "summary": f"Petición {n}: {text}"[:140],
        "text": text,
        "segment_ids": [f"seg-{n}a", f"seg-{n}b"],
        "t_start_ms": 1_000 * n,
        "t_end_ms": 1_000 * n + 800,
        "detector": "observer",
    }


def _publish(client: TestClient, session_id: str, payload: dict[str, Any]) -> None:
    bus = client.app.state.bus  # type: ignore[attr-defined]

    async def publish() -> None:
        await bus.publish(session_id, ASSISTANT_REQUEST_KIND, "observer", payload)

    client.portal.call(publish)  # type: ignore[union-attr]


def _settle(client: TestClient) -> None:
    consumer: AssistantRequestConsumer = client.app.state.assistant_requests  # type: ignore[attr-defined]
    client.portal.call(consumer.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]


def _subscribe(client: TestClient, topic: ReviseTopic) -> WorkspaceSubscription:
    hub = client.app.state.workspace  # type: ignore[attr-defined]
    return hub.subscribe(topic.subject, topic.topic)  # type: ignore[no-any-return]


def _names(events: list[WorkspaceEvent]) -> list[str]:
    """The event names, consecutive `reply.delta`s folded into one."""
    names: list[str] = []
    for event in events:
        if event.event == "reply.delta" and names and names[-1] == "reply.delta":
            continue
        names.append(event.event)
    return names


def _edit(fake: FakeClaude, summary: str = "Añado la explicación del libro") -> None:
    fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": summary,
            "ops": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 1,
                    "text": f"{summary}: es el límite de (f(a+h) - f(a)) / h.[^b1]",
                }
            ],
            "footnotes": [{"label": "b1", "definition": BOOK_FOOTNOTE}],
        },
        text=f"{summary}.",
    )


def test_a_spoken_request_becomes_a_voice_chat_turn_and_streams(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _edit(fake)

    _publish(client, session_id, _request(1))
    _settle(client)

    events = subscription.drain()
    assert _names(events) == [
        "request.detected",
        "turn.started",
        "reply.delta",
        "turn.result",
        "notes.changed",
    ]
    detected, started, *_rest, result, changed = events
    assert detected.data == {
        "request_id": "req-1",
        "kind": "edit",
        "summary": "Petición 1: pon aquí la explicación del libro",
        "transcript": {
            "session_id": session_id,
            "segment_ids": ["seg-1a", "seg-1b"],
            "t_start_ms": 1000,
            "t_end_ms": 1800,
            "text": "pon aquí la explicación del libro",
        },
    }
    turn_id = started.data["turn_id"]
    assert started.data == {
        "turn_id": turn_id,
        "request_id": "req-1",
        "origin": "voice",
        "kind": "revise",
    }
    assert all(e.data["turn_id"] == turn_id for e in events if e.event == "reply.delta")
    assert result.data["turn_id"] == turn_id and result.data["request_id"] == "req-1"
    assert result.data["applied"] and result.data["origin"] == "voice"
    notes = read_notes(topic.vault, topic.subject, topic.topic) or ""
    assert "Añado la explicación del libro: es el límite" in notes
    assert changed.data == {
        "revision": notes_revision(notes),
        "origin": "editor",
        "summary": "Añado la explicación del libro",
        "turn_id": turn_id,
    }
    # The raw transcript is the message, marked as spoken.
    sent = json.dumps(fake.requests[0].messages, ensure_ascii=False)
    assert "pon aquí la explicación del libro" in sent and "en voz alta" in sent

    history = client.get(_chat(topic)).json()
    [turn] = history["turns"]
    assert turn["origin"] == "voice" and turn["turn_id"] == turn_id
    assert turn["message"] == "pon aquí la explicación del libro"
    assert turn["request_summary"] == "Petición 1: pon aquí la explicación del libro"
    assert turn["summary"] == "Añado la explicación del libro"
    assert turn["transcript"] == {
        "request_id": "req-1",
        "summary": "Petición 1: pon aquí la explicación del libro",
        "session_id": session_id,
        "segment_ids": ["seg-1a", "seg-1b"],
        "t_start_ms": 1000,
        "t_end_ms": 1800,
        "text": "pon aquí la explicación del libro",
    }
    assert history["can_undo"] is True


def test_two_requests_run_one_at_a_time_in_order(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _edit(fake, "Primer cambio")
    fake.reply_text("La derivada mide el cambio instantáneo.")
    consumer: AssistantRequestConsumer = client.app.state.assistant_requests  # type: ignore[attr-defined]

    async def both() -> None:
        # Published back to back: the second arrives while the first is queued or running.
        bus = client.app.state.bus  # type: ignore[attr-defined]
        await bus.publish(session_id, ASSISTANT_REQUEST_KIND, "observer", _request(1))
        await bus.publish(
            session_id,
            ASSISTANT_REQUEST_KIND,
            "observer",
            _request(2, "question", "qué es la derivada"),
        )
        await consumer.wait_idle(WAIT_SECONDS)

    client.portal.call(both)  # type: ignore[union-attr]

    events = subscription.drain()
    detected = [e.data["request_id"] for e in events if e.event == "request.detected"]
    started = [e.data["request_id"] for e in events if e.event == "turn.started"]
    results = [e.data["request_id"] for e in events if e.event == "turn.result"]
    assert detected == started == results == ["req-1", "req-2"]
    # The second turn started only after the first one's result.
    names = [(e.event, e.data.get("request_id")) for e in events]
    assert names.index(("turn.result", "req-1")) < names.index(("turn.started", "req-2"))
    # The second request saw the first turn in its history.
    second = json.dumps(fake.requests[1].messages, ensure_ascii=False)
    assert "Primer cambio" in second and "qué es la derivada" in second
    turns = client.get(_chat(topic)).json()["turns"]
    assert [t["message"] for t in turns] == [
        "pon aquí la explicación del libro",
        "qué es la derivada",
    ]
    assert [t["origin"] for t in turns] == ["voice", "voice"]
    assert turns[1]["applied"] is False


def test_a_request_of_an_ended_session_is_still_processed(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    _edit(fake)
    _publish(client, session_id, _request(1))
    ended = client.post(
        f"/api/sessions/{session_id}/end", json={"client_time_ms": 2_000, "reason": "button"}
    )
    assert ended.status_code == 200, ended.text

    _settle(client)

    [turn] = client.get(_chat(topic)).json()["turns"]
    assert turn["origin"] == "voice" and turn["applied"] is True


def test_a_typed_turn_is_broadcast_on_the_workspace(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    subscription = _subscribe(client, topic)
    _edit(fake)

    response = client.post(_chat(topic), json={"message": "Usa la explicación del libro"})

    assert response.status_code == 200, response.text
    assert '"turn_id"' in response.text  # the caller's own stream is unchanged, plus the id
    events = subscription.drain()
    assert _names(events) == ["turn.started", "reply.delta", "turn.result", "notes.changed"]
    started = events[0]
    assert started.data["origin"] == "typed" and started.data["request_id"] is None
    result = events[-2].data
    assert result["turn_id"] == started.data["turn_id"] and result["origin"] == "typed"
    assert events[-1].data["origin"] == "editor"
    [turn] = client.get(_chat(topic)).json()["turns"]
    assert turn["origin"] == "typed" and turn["turn_id"] == started.data["turn_id"]
    assert turn["request_summary"] is None and turn["transcript"] is None

    undo = client.post(f"{_chat(topic)}/undo")
    assert undo.status_code == 200, undo.text
    [undone] = subscription.drain()
    assert undone.event == "notes.changed" and undone.data["origin"] == "editor"
    assert undone.data["revision"] == notes_revision(topic.notes)


def test_a_typed_turn_error_is_broadcast(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    with _client(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        subscription = _subscribe(client, topic)
        response = client.post(_chat(topic), json={"message": "Pon un ejemplo"})
        assert response.status_code == 200
        events = subscription.drain()
    assert _names(events) == ["turn.started", "turn.error"]
    error = events[-1].data
    assert error["status"] == 409 and error["code"] == "cost_cap_reached"
    assert error["turn_id"] == events[0].data["turn_id"]


def test_a_student_save_emits_notes_changed_from_the_user(
    client: TestClient, topic: ReviseTopic
) -> None:
    subscription = _subscribe(client, topic)
    text = topic.notes.replace("Se escribe", "Se suele escribir")

    saved = client.put(
        f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes",
        json={"text": text, "base_revision": notes_revision(topic.notes)},
    )

    assert saved.status_code == 200, saved.text
    [changed] = subscription.drain()
    assert changed.event == "notes.changed"
    assert changed.data["origin"] == "user"
    assert changed.data["revision"] == saved.json()["revision"]
    assert changed.data["summary"].startswith("Has editado los apuntes")


def test_prepare_notes_starts_a_generation(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    notes = valid_notes(topic.session).replace("Se escribe", "Normalmente se escribe")
    fake.reply_text(notes)

    _publish(client, session_id, _request(1, "prepare_notes", "prepárame el tema"))
    _settle(client)

    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "notes.changed", "turn.result"]
    assert events[1].data["kind"] == "prepare_notes" and events[1].data["origin"] == "voice"
    assert events[2].data["origin"] == "generation"
    assert events[2].data["revision"] == notes_revision(notes)
    result = events[3].data
    assert result["kind"] == "prepare_notes" and result["request_id"] == "req-1"
    assert result["draft"] is False and result["version"] is not None
    assert read_notes(topic.vault, topic.subject, topic.topic) == notes
    status = client.get(
        f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generation"
    ).json()
    assert status["status"] == "done"
    assert fake.requests[0].role == "editor"


def test_a_reached_cap_is_a_turn_error_never_a_spend(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    fake.reply_text(valid_notes(topic.session))
    with _client(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        session_id = _start(client, topic)
        subscription = _subscribe(client, topic)

        _publish(client, session_id, _request(1, "prepare_notes", "prepárame el tema"))
        _settle(client)

        events = subscription.drain()
        # The lock was released: the next request of the topic runs (and is capped too).
        _publish(client, session_id, _request(2))
        _settle(client)
        second = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "turn.error"]
    error = events[-1].data
    assert error["status"] == 409 and error["code"] == "cost_cap_reached"
    assert error["request_id"] == "req-1" and "Confirma" in error["detail"]
    assert fake.requests == []
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
    assert _names(second)[-1] == "turn.error" and second[-1].data["code"] == "cost_cap_reached"


def test_a_malformed_request_is_ignored(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _publish(client, session_id, {"request_id": "nope", "kind": "edit"})
    _settle(client)
    assert subscription.drain() == []
    assert fake.requests == []


def test_a_request_waits_for_a_busy_topic(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    generator = client.app.state.notes  # type: ignore[attr-defined]
    assert generator.claim(topic.subject, topic.topic, "editor")  # e.g. a restore running
    _edit(fake)
    _publish(client, session_id, _request(1))

    deadline = time.monotonic() + WAIT_SECONDS
    while not any(e.event == "request.detected" for e in subscription.drain()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.3)
    assert fake.requests == []  # still waiting for the lock
    generator.release(topic.subject, topic.topic)
    _settle(client)

    assert [e.event for e in subscription.drain()][-1] == "notes.changed"
    assert len(fake.requests) == 1
