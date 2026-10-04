"""Spoken requests become editor turns (`server/assistant_requests.py`) and the workspace hub feed.

Every test runs the real app (`FakeClaude`, `tmp_vault`, the FastAPI test client): an
`assistant.request` is published on the bus of an active session, as the observer's detector does,
and the turn is read back from `GET .../notes/chat` and from a workspace hub subscription.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
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
from studentassistant.editor.crop import CHECK_TOOL_NAME, CROP_TOOL, TOOL_NAME
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.revise import EDIT_TOOL, ChatRequestRef, ChatTurn
from studentassistant.llm import FakeClaude, LLMAPIError
from studentassistant.observer import ASSISTANT_REQUEST_KIND, REQUEST_KINDS
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server import assistant_requests
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import (
    HANDLERS,
    REQUESTS_OUTSTANDING_KIND,
    TURN_FINISHED_KIND,
    TURN_KINDS,
    UNKNOWN_KIND_DETAIL,
    AssistantRequestConsumer,
    outstanding_requests,
    unanswered_requests,
)
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.vault import (
    Event,
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    get_subject,
    get_topic,
    list_sessions,
    put_source,
    read_notes,
    read_topic_events,
    resume_session,
    start_session,
    write_notes,
)

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
AppFactory = Callable[..., FastAPI]


def _topic_in_the_user_folder(root: Vault, user: Vault, topic: ReviseTopic) -> None:
    """Create the fixture topic's subject and topic under the vault's one user as well.

    `POST /api/sessions` acts for that user (#550), so the session it starts -- and every event
    published on it -- lives under `users/<id>/`, which needs the subject and the topic there.
    The notes, the sources and the review sessions the routes and the consumer read and write
    still go through the repository root (`SessionService.open_vault()`) until #551.
    """
    subject = create_subject(user, get_subject(root, topic.subject).subject.name).slug
    create_topic(user, subject, get_topic(root, subject, topic.topic).topic.title)


@pytest.fixture
def topic(tmp_vault: Vault, user_vault: Vault) -> ReviseTopic:
    built = make_revise_topic(tmp_vault)
    _topic_in_the_user_folder(tmp_vault, user_vault, built)
    return built


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
                editor=EditorSettings(doubts_in_chat=False, prepare_mode="single"),
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
        "origin": "voice",
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


def _diagram() -> bytes:
    """A sharp line drawing on white paper, as a JPEG."""
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def test_a_spoken_crop_request_locates_the_region_with_the_apps_own_transport(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    # A spoken request carries no Recursos selection: the page to crop is one the notes cite.
    book_page = "sources/book/page-002.jpg"
    put_source(topic.vault, topic.subject, topic.topic, "book", "foto.jpg", _diagram(), {})
    notes = topic.notes.replace(
        "Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2][^b2]"
    ).replace("[^p1]: [Apuntes", f"[^b2]: [Libro, página 2](../{book_page})\n[^p1]: [Apuntes")
    write_notes(topic.vault, topic.subject, topic.topic, notes)
    GitSync(topic.vault).checkpoint("fixture: cite page 2")
    session_id = _start(client, topic)
    crop_call = {
        "source": book_page,
        "region": "el diagrama de la página",
        "op": "insert_after",
        "section": "definicion",
        "block": 2,
        "summary": "Añado el recorte del diagrama",
    }
    fake.reply_tool(CROP_TOOL, crop_call, text="He añadido el recorte del diagrama.")
    fake.reply_tool(TOOL_NAME, {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75})
    # The refined box on the zoomed page and the check of the cut (#520).
    fake.reply_tool(TOOL_NAME, {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9})
    complete = {"complete": True, "left": "ok", "top": "ok", "right": "ok", "bottom": "ok"}
    fake.reply_tool(CHECK_TOOL_NAME, complete)

    _publish(client, session_id, _request(1, text="pon solo el diagrama de la página 2"))
    _settle(client)

    # Sonnet's box came from the app's own transport (a default connection would never reach
    # the fake), and the crop was applied as the voice turn's change.
    assert [request.role for request in fake.requests] == ["editor", *["observer"] * 3]
    assert fake.requests[1].tools[0]["name"] == TOOL_NAME and fake.pending == 0
    assert fake.requests[3].tools[0]["name"] == CHECK_TOOL_NAME
    [turn] = client.get(_chat(topic)).json()["turns"]
    assert turn["origin"] == "voice" and turn["applied"] is True
    assert turn["crop"]["error"] is None
    assert turn["crop"]["source_id"] == "sources/images/img-001.jpg"
    stored = read_notes(topic.vault, topic.subject, topic.topic) or ""
    assert "![Imagen recortada 1](../sources/images/img-001.jpg)[^img001]" in stored


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


# -- a backend restart (#408) --------------------------------------------------------------------


def _resume(client: TestClient, session_id: str) -> None:
    resumed = client.post(f"/api/sessions/{session_id}/resume")
    assert resumed.status_code == 200, resumed.text


def _events(vault: Vault, topic: ReviseTopic, session_id: str, kind: str) -> list[dict[str, Any]]:
    """The payloads of `kind` in the topic's unended session, read back from the vault.

    `vault` is the handle that session lives in: the one user's, because `POST /api/sessions`
    acts for that user and the events published on the session go to its log (#550).
    """
    session = resume_session(vault, topic.subject, topic.topic)
    assert session.id == session_id
    return [dict(e.payload) for e in session.read_events() if e.kind == kind]


def test_a_request_queued_at_shutdown_is_answered_after_a_restart(
    make_app: AppFactory, topic: ReviseTopic, user_vault: Vault
) -> None:
    first = FakeClaude()
    _edit(first, "Primer cambio")
    with _client(make_app(first)) as client:
        session_id = _start(client, topic)
        _publish(client, session_id, _request(1))
        _settle(client)
        # Something holds the notes, so the second request is still queued at shutdown.
        generator = client.app.state.notes  # type: ignore[attr-defined]
        assert generator.claim(topic.subject, topic.topic, "editor")
        subscription = _subscribe(client, topic)
        _publish(client, session_id, _request(2, "question", "qué es la derivada"))
        deadline = time.monotonic() + WAIT_SECONDS
        while not any(e.event == "request.detected" for e in subscription.drain()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        consumer: AssistantRequestConsumer = client.app.state.assistant_requests  # type: ignore[attr-defined]
        client.portal.call(consumer.stop, 0.1)  # type: ignore[union-attr]
        generator.release(topic.subject, topic.topic)
    assert len(first.requests) == 1
    finished = _events(user_vault, topic, session_id, TURN_FINISHED_KIND)
    assert [(f["request_id"], f["outcome"]) for f in finished] == [("req-1", "result")]

    second = FakeClaude()
    second.reply_text("La derivada mide el cambio instantáneo.")
    with _client(make_app(second)) as client:
        subscription = _subscribe(client, topic)
        _resume(client, session_id)
        _settle(client)
        events = subscription.drain()
        assert [e.data["request_id"] for e in events if e.event == "request.detected"] == ["req-2"]
        assert [e.data["request_id"] for e in events if e.event == "turn.result"] == ["req-2"]
        assert len(second.requests) == 1
        assert "qué es la derivada" in json.dumps(second.requests[0].messages, ensure_ascii=False)
        turns = client.get(_chat(topic)).json()["turns"]
        assert [t["transcript"]["request_id"] for t in turns] == ["req-1", "req-2"]

        # Resumed again (a reconnect): nothing is replayed twice.
        _resume(client, session_id)
        _settle(client)
        assert not any(e.event == "request.detected" for e in subscription.drain())
        assert len(second.requests) == 1

    # A third backend finds every request answered.
    third = FakeClaude()
    with _client(make_app(third)) as client:
        subscription = _subscribe(client, topic)
        _resume(client, session_id)
        _settle(client)
        assert subscription.drain() == []
        assert third.requests == []


# -- requests of an ended session across a restart (#423) ------------------------------------------


def _held(client: TestClient, topic: ReviseTopic) -> Any:
    """Take the topic's notes lock, so the next request waits in the queue; the generator."""
    generator = client.app.state.notes  # type: ignore[attr-defined]
    assert generator.claim(topic.subject, topic.topic, "editor")
    return generator


def _wait_detected(subscription: WorkspaceSubscription) -> list[WorkspaceEvent]:
    deadline = time.monotonic() + WAIT_SECONDS
    seen: list[WorkspaceEvent] = []
    while not any(e.event == "request.detected" for e in seen):
        assert time.monotonic() < deadline, "no request.detected"
        seen += subscription.drain()
        time.sleep(0.01)
    return seen


def _shut_down(client: TestClient) -> None:
    consumer: AssistantRequestConsumer = client.app.state.assistant_requests  # type: ignore[attr-defined]
    client.portal.call(consumer.stop, 0.1)  # type: ignore[union-attr]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _review_events(topic: ReviseTopic, kind: str) -> list[dict[str, Any]]:
    """The payloads of `kind` in the topic's review sessions, read back from the vault."""
    reviews = {
        m.id for m in list_sessions(topic.vault, topic.subject, topic.topic) if not m.is_study
    }
    return [
        dict(event.payload)
        for session_id, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if session_id in reviews and event.kind == kind
    ]


def _restart(
    make_app: AppFactory, topic: ReviseTopic, transport: FakeClaude
) -> tuple[list[WorkspaceEvent], list[dict[str, Any]]]:
    """A new backend: open the vault (a first request), let it catch up; its stream and chat."""
    with _client(make_app(transport)) as client:
        subscription = _subscribe(client, topic)
        assert client.get(_chat(topic)).status_code == 200  # opens the vault: the catch-up runs
        _settle(client)
        return subscription.drain(), client.get(_chat(topic)).json()["turns"]


def test_a_typed_request_of_a_review_session_is_answered_after_a_restart(
    make_app: AppFactory, topic: ReviseTopic
) -> None:
    first = FakeClaude()
    first.fail(LLMAPIError("boom"))  # the classifier fails: the text is kept as a question
    with _client(make_app(first)) as client:
        generator = _held(client, topic)
        subscription = _subscribe(client, topic)
        response = client.post(
            f"/api/subjects/{topic.subject}/topics/{topic.topic}/workspace/messages",
            json={"text": "¿qué es la derivada?"},
        )
        assert response.status_code == 202, response.text
        [request] = response.json()["requests"]
        assert request["request_id"] == "req-t1" and request["kind"] == "question"
        _wait_detected(subscription)
        _shut_down(client)
        generator.release(topic.subject, topic.topic)
    assert len(first.requests) == 1  # the classification only: the turn never ran
    [outstanding] = _review_events(topic, REQUESTS_OUTSTANDING_KIND)
    assert outstanding == {"request_ids": ["req-t1"]}
    assert _review_events(topic, TURN_FINISHED_KIND) == []

    second = FakeClaude()
    second.reply_text("La derivada mide el cambio instantáneo.")
    events, turns = _restart(make_app, topic, second)
    assert [e.data["request_id"] for e in events if e.event == "request.detected"] == ["req-t1"]
    assert [e.data["request_id"] for e in events if e.event == "turn.result"] == ["req-t1"]
    assert [e.data["origin"] for e in events if e.event == "turn.started"] == ["typed"]
    assert len(second.requests) == 1
    assert "qué es la derivada" in json.dumps(second.requests[0].messages, ensure_ascii=False)
    assert [(t["origin"], t["message"]) for t in turns] == [("typed", "¿qué es la derivada?")]
    [finished] = _review_events(topic, TURN_FINISHED_KIND)
    assert finished["request_id"] == "req-t1" and finished["outcome"] == "result"

    # A third backend finds it answered: exactly once.
    third = FakeClaude()
    events, turns = _restart(make_app, topic, third)
    assert not any(e.event == "request.detected" for e in events)
    assert third.requests == [] and len(turns) == 1


def test_a_request_of_a_session_ended_before_its_turn_is_answered_after_a_restart(
    make_app: AppFactory, topic: ReviseTopic, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_review = assistant_requests._review_session

    def slow_review(*args: Any) -> str:
        time.sleep(0.3)  # a slow disk: the session's end still waits for the record (#468)
        return write_review(*args)

    monkeypatch.setattr(assistant_requests, "_review_session", slow_review)
    first = FakeClaude()
    with _client(make_app(first)) as client:
        session_id = _start(client, topic)
        generator = _held(client, topic)
        subscription = _subscribe(client, topic)
        _publish(client, session_id, _request(1, "question", "qué es la derivada"))
        _wait_detected(subscription)
        ended = client.post(
            f"/api/sessions/{session_id}/end", json={"client_time_ms": 2_000, "reason": "button"}
        )
        assert ended.status_code == 200, ended.text
        # Recorded as part of the end (#468): written, and committed with "sesión ... terminada".
        assert _review_events(topic, REQUESTS_OUTSTANDING_KIND)
        review = next(
            m.id for m in list_sessions(topic.vault, topic.subject, topic.topic) if not m.is_study
        )
        last_commit = _git(topic.vault.path, "log", "-1", "--format=%s")
        assert last_commit == f"sesión {session_id} terminada"
        assert _git(topic.vault.path, "status", "--porcelain", "--", f"*{review}*") == ""
        _shut_down(client)
        generator.release(topic.subject, topic.topic)
    assert first.requests == []
    [outstanding] = _review_events(topic, REQUESTS_OUTSTANDING_KIND)
    assert outstanding == {"session_id": session_id, "request_ids": ["req-1"]}

    second = FakeClaude()
    second.reply_text("La derivada mide el cambio instantáneo.")
    events, turns = _restart(make_app, topic, second)
    assert [e.data["request_id"] for e in events if e.event == "turn.result"] == ["req-1"]
    assert len(second.requests) == 1
    [turn] = turns
    assert turn["origin"] == "voice"
    assert (turn["transcript"]["session_id"], turn["transcript"]["request_id"]) == (
        session_id,
        "req-1",
    )
    [finished] = _review_events(topic, TURN_FINISHED_KIND)
    assert (finished["session_id"], finished["request_id"]) == (session_id, "req-1")

    third = FakeClaude()
    events, turns = _restart(make_app, topic, third)
    assert events == [] and third.requests == [] and len(turns) == 1


def test_outstanding_requests_skip_the_answered_and_the_unknown(topic: ReviseTopic) -> None:
    vault, s, t = topic.vault, topic.subject, topic.topic
    capture = start_session(vault, s, t, "host", PROTOCOL_VERSION)
    for n in (1, 2, 3):
        capture.append_event(ASSISTANT_REQUEST_KIND, "observer", _request(n))
    end_session(capture)

    def review(*events: tuple[str, dict[str, Any]]) -> str:
        session = start_session(vault, s, t, "host", PROTOCOL_VERSION, kind="review")
        for kind, payload in events:
            session.append_event(kind, "editor", payload)
        end_session(session)
        return session.id

    finished = {"turn_id": "t", "kind": "revise", "outcome": "result"}
    review(
        (REQUESTS_OUTSTANDING_KIND, {"session_id": capture.id, "request_ids": ["req-1", "req-2"]}),
        (REQUESTS_OUTSTANDING_KIND, {"session_id": capture.id, "request_ids": ["req-3", "req-9"]}),
    )
    review((TURN_FINISHED_KIND, {"request_id": "req-2", "session_id": capture.id, **finished}))
    # The same id of another session answers nothing here.
    review((TURN_FINISHED_KIND, {"request_id": "req-3", "session_id": "other", **finished}))
    # An `outstanding` in a study session is not the backend's record: ignored.
    study = start_session(vault, s, t, "host", PROTOCOL_VERSION)
    study.append_event(REQUESTS_OUTSTANDING_KIND, "editor", {"request_ids": ["req-x"]})
    end_session(study)

    pending = outstanding_requests(vault, s, t)
    assert [(sid, r.request_id) for sid, r in pending] == [
        (capture.id, "req-1"),
        (capture.id, "req-3"),  # req-9 has no assistant.request: left out
    ]


def test_unanswered_requests_follow_the_newest_answered_one() -> None:
    def request(n: int, seq: int) -> Event:
        return Event(
            seq=seq, t=seq, origin="observer", kind=ASSISTANT_REQUEST_KIND, payload=_request(n)
        )

    def finished(n: int, seq: int) -> Event:
        return Event(
            seq=seq,
            t=seq,
            origin="editor",
            kind=TURN_FINISHED_KIND,
            payload={
                "request_id": f"req-{n}",
                "turn_id": "t",
                "kind": "revise",
                "outcome": "result",
            },
        )

    malformed = Event(
        seq=9, t=9, origin="observer", kind=ASSISTANT_REQUEST_KIND, payload={"request_id": "x"}
    )
    # req-1 has no record but ran before the answered req-2 (a failure before #408).
    events = [request(1, 1), request(2, 2), finished(2, 3), request(3, 4), request(4, 5), malformed]
    assert [r.request_id for r in unanswered_requests(events, "s1")] == ["req-3", "req-4"]
    # A voice chat turn of req-3 of this session answers it; one of another session does not.
    voice = ChatTurn(
        time=datetime(2026, 9, 27, tzinfo=UTC),
        message="m",
        reply="r",
        origin="voice",
        transcript=ChatRequestRef(
            request_id="req-4",
            summary="s",
            session_id="s1",
            segment_ids=["a"],
            t_start_ms=0,
            t_end_ms=1,
            text="m",
        ),
    )
    other = voice.model_copy(
        update={"transcript": voice.transcript.model_copy(update={"session_id": "s0"})}  # type: ignore[union-attr]
    )
    assert [r.request_id for r in unanswered_requests(events, "s1", [other])] == ["req-3", "req-4"]
    assert unanswered_requests(events, "s1", [voice]) == []
    assert unanswered_requests([], "s1") == []


def test_every_request_kind_has_a_handler_and_a_turn_kind() -> None:
    assert set(HANDLERS) == set(REQUEST_KINDS)
    assert set(TURN_KINDS) == set(REQUEST_KINDS)


def test_a_kind_without_a_handler_is_a_turn_error(
    client: TestClient,
    fake: FakeClaude,
    topic: ReviseTopic,
    user_vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(assistant_requests.HANDLERS, "question")  # type: ignore[arg-type]
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _publish(client, session_id, _request(1, "question", "qué es la derivada"))
    _settle(client)

    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.error"]
    error = events[-1].data
    assert error["status"] == 422 and error["detail"] == UNKNOWN_KIND_DETAIL
    assert error["request_id"] == "req-1"
    assert fake.requests == []
    [finished] = _events(user_vault, topic, session_id, TURN_FINISHED_KIND)
    assert finished["outcome"] == "error" and finished["status"] == 422


def test_a_failing_event_does_not_stop_the_reader(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit = AssistantRequestConsumer.submit

    def failing(self: AssistantRequestConsumer, *args: Any) -> bool:
        if args[-1].request_id == "req-1":
            raise RuntimeError("boom")
        return submit(self, *args)

    monkeypatch.setattr(AssistantRequestConsumer, "submit", failing)
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _edit(fake)
    _publish(client, session_id, _request(1))
    _publish(client, session_id, _request(2))
    _settle(client)

    events = subscription.drain()
    assert [e.data["request_id"] for e in events if e.event == "turn.result"] == ["req-2"]
