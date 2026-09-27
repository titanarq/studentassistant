"""Capture sessions end themselves when no client is sending (#425, `server/capture_liveness.py`).

The watchdog's clock is a `FakeClock` and its loop is slowed to an hour, so every check is an
explicit `tick()` run on the app's event loop (the TestClient is entered, so sockets, requests and
the tick share it). No fixed sleep: a socket's earlier messages are known handled once the echo of
a later transcript segment comes back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from ws_harness import WsHarness

from conftest import LOCAL_BASE_URL, LOOPBACK_HOST, FakeClock, HostedTestClient
from studentassistant.config import ObserverSettings, ServerSettings, Settings, SttSettings
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.capture_liveness import IDLE_CLOSE_MARK, CaptureLiveness
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import OpenSession, SessionAlreadyEndedError
from studentassistant.server.ws import CLOSE_UNKNOWN_SESSION
from studentassistant.vault import Event, Vault, read_jsonl, read_notes

GRACE = 300.0
RECEIVE_TIMEOUT_S = 5.0
HELLO_AT = 1_000_000


class Live:
    """The app, its entered loopback client, the watchdog and its clock."""

    def __init__(self, app: FastAPI, client: HostedTestClient, clock: FakeClock, vault: Vault):
        self.app = app
        self.client = client
        self.clock = clock
        self.vault = vault
        self.session_id = ""

    @property
    def liveness(self) -> CaptureLiveness:
        return self.app.state.liveness

    def start(self) -> str:
        response = self.client.post(
            "/api/sessions",
            json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1_000},
        )
        assert response.status_code == 201, response.text
        self.session_id = response.json()["session_id"]
        return self.session_id

    def tick(self) -> str | None:
        async def bounded() -> str | None:
            with anyio.fail_after(RECEIVE_TIMEOUT_S * 2):
                return await self.liveness.tick()

        return self.client.portal.call(bounded)  # type: ignore[union-attr]

    def connect(self) -> Any:
        return self.client.websocket_connect(f"/ws/sessions/{self.session_id}")

    def events(self, session_id: str | None = None) -> list[Event]:
        log = (
            self.vault.path
            / "subjects/fisica/topics/cinematica/sessions"
            / (session_id or self.session_id)
            / "events.jsonl"
        )
        return list(read_jsonl(log, Event))

    def ended(self) -> list[Event]:
        return [e for e in self.events() if e.kind == "session.ended"]


def receive(socket: Any, timeout: float = RECEIVE_TIMEOUT_S) -> dict[str, Any]:
    """The next raw message the backend sent `socket`, failing the test after `timeout`."""

    async def read() -> Any:
        with anyio.fail_after(timeout):
            return await socket._send_rx.receive()

    try:
        return socket.portal.call(read)
    except TimeoutError:
        pytest.fail(f"no server message within {timeout} s")


def hello(socket: Any) -> None:
    socket.send_json(WsHarness.hello(HELLO_AT))
    message = receive(socket)
    assert json.loads(message["text"])["type"] == "hello.ack"


def button(socket: Any, name: str) -> None:
    socket.send_json({"type": "button", "button": name, "client_time_ms": HELLO_AT + 1})


_segments = iter(range(1, 10_000))


def sync(socket: Any) -> None:
    """Send a client segment and wait for its echo: everything sent before it was handled."""
    segment_id = f"sync-{next(_segments)}"
    socket.send_json(
        {
            "type": "transcript.client.final",
            "segment_id": segment_id,
            "client_start_ms": HELLO_AT,
            "client_end_ms": HELLO_AT + 100,
            "text": "hola",
            "provider": "web-speech",
            "language": "es-ES",
        }
    )
    while True:
        message = json.loads(receive(socket)["text"])
        if message.get("segment_id") == segment_id:
            return


def make_app(
    tmp_path: Path, devices_path: Path, codes: PairingCodes, vault: Vault, **kwargs: Any
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path, capture_idle_end_seconds=GRACE),
        codes=codes,
        vault=vault,
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
        **kwargs,
    )


def enter(app: FastAPI, vault: Vault) -> Iterator[Live]:
    clock = FakeClock()
    app.state.liveness.clock = clock
    app.state.liveness.interval = 3600.0  # only explicit ticks
    with HostedTestClient(app, base_url=LOCAL_BASE_URL, client=(LOOPBACK_HOST, 50000)) as client:
        assert client.post("/api/subjects", json={"name": "Física"}).status_code == 201
        topic = client.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
        assert topic.status_code == 201
        yield Live(app, client, clock, vault)


@pytest.fixture
def live(
    tmp_path: Path, devices_path: Path, codes: PairingCodes, tmp_vault: Vault
) -> Iterator[Live]:
    yield from enter(make_app(tmp_path, devices_path, codes, tmp_vault), tmp_vault)


# -- the backend's own end ------------------------------------------------------------------------


def test_a_session_no_socket_ever_joins_ends_after_the_grace_period(live: Live) -> None:
    session_id = live.start()
    live.clock.advance(GRACE - 1)
    assert live.tick() is None
    assert live.ended() == []

    live.clock.advance(1)
    assert live.tick() == session_id

    [ended] = live.ended()
    assert ended.payload["reason"] == "idle"
    assert ended.payload["idle_seconds"] == GRACE
    assert ended.origin == "user"
    assert live.app.state.sessions.active is None
    assert live.liveness.ended_idle(session_id)


def test_a_connected_sending_socket_keeps_the_session(live: Live) -> None:
    live.start()
    with live.connect() as socket:
        hello(socket)
        live.clock.advance(GRACE * 10)
        assert live.tick() is None
    assert live.ended() == []
    assert live.app.state.sessions.active is not None


def test_a_paused_socket_past_the_grace_period_ends_the_session(live: Live) -> None:
    session_id = live.start()
    with live.connect() as socket:
        hello(socket)
        live.clock.advance(GRACE * 2)  # sending meanwhile: no idle time
        button(socket, "pause")
        sync(socket)
        live.clock.advance(GRACE - 1)
        assert live.tick() is None
        live.clock.advance(1)
        assert live.tick() == session_id
        # The still-open socket is closed as not active on its next message, saying why.
        button(socket, "resume")
        closed = receive(socket)

    assert closed["type"] == "websocket.close"
    assert closed["code"] == CLOSE_UNKNOWN_SESSION
    assert closed["reason"].endswith(IDLE_CLOSE_MARK)
    [ended] = live.ended()
    assert ended.payload["reason"] == "idle"
    assert ended.payload["idle_seconds"] == GRACE
    buttons = [e.payload["button"] for e in live.events() if e.kind == "button"]
    assert buttons == ["pause"]


def test_pause_then_resume_before_the_grace_period_keeps_the_session(live: Live) -> None:
    live.start()
    with live.connect() as socket:
        hello(socket)
        button(socket, "pause")
        sync(socket)
        live.clock.advance(GRACE - 10)
        button(socket, "resume")
        sync(socket)
        live.clock.advance(GRACE * 2)
        assert live.tick() is None
    assert live.ended() == []


def test_a_socket_that_reconnects_within_the_grace_period_keeps_the_session(live: Live) -> None:
    session_id = live.start()
    with live.connect() as socket:
        hello(socket)
    live.clock.advance(GRACE - 10)
    assert live.tick() is None
    with live.connect() as socket:
        hello(socket)
        live.clock.advance(GRACE * 2)
        assert live.tick() is None
    assert live.ended() == []
    # Gone again for the whole grace period: now it ends.
    live.clock.advance(GRACE)
    assert live.tick() == session_id


def test_the_grace_period_restarts_on_resume(live: Live) -> None:
    session_id = live.start()
    live.clock.advance(GRACE - 10)
    assert live.client.post(f"/api/sessions/{session_id}/resume").status_code == 200
    live.clock.advance(GRACE - 10)
    assert live.tick() is None
    live.clock.advance(10)
    assert live.tick() == session_id


def test_a_new_start_on_the_topic_succeeds_after_the_idle_end(live: Live) -> None:
    first = live.start()
    live.clock.advance(GRACE)
    assert live.tick() == first
    # A socket of the ended session is refused, saying why.
    with live.connect() as socket:
        closed = receive(socket)
    assert closed["code"] == CLOSE_UNKNOWN_SESSION
    assert closed["reason"].endswith(IDLE_CLOSE_MARK)

    second = live.start()
    assert second != first
    assert live.app.state.sessions.active.session_id == second
    # The new session gets a grace period of its own.
    assert live.tick() is None


def test_an_explicit_end_leaves_nothing_for_the_watchdog(live: Live) -> None:
    session_id = live.start()
    ended = live.client.post(
        f"/api/sessions/{session_id}/end", json={"client_time_ms": 2_000, "reason": "button"}
    )
    assert ended.status_code == 200
    live.clock.advance(GRACE * 2)
    assert live.tick() is None
    assert [e.payload["reason"] for e in live.ended()] == ["button"]
    assert not live.liveness.ended_idle(session_id)


def test_the_watchdog_runs_with_the_lifespan(
    tmp_path: Path, devices_path: Path, codes: PairingCodes, tmp_vault: Vault
) -> None:
    app = make_app(tmp_path, devices_path, codes, tmp_vault)
    liveness: CaptureLiveness = app.state.liveness
    assert liveness.grace_seconds == GRACE
    with HostedTestClient(app, base_url=LOCAL_BASE_URL, client=(LOOPBACK_HOST, 50000)):
        assert liveness._task is not None and not liveness._task.done()
    assert liveness._task is None


# -- no generation ----------------------------------------------------------------------------


def test_an_idle_end_generates_nothing(
    tmp_path: Path, devices_path: Path, codes: PairingCodes, tmp_vault: Vault
) -> None:
    fake = FakeClaude()
    app = make_app(
        tmp_path,
        devices_path,
        codes,
        tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    for live in enter(app, tmp_vault):
        session_id = live.start()
        live.clock.advance(GRACE)
        assert live.tick() == session_id
        generator = app.state.notes
        assert generator is not None
    assert fake.requests == []
    assert read_notes(tmp_vault, "fisica", "cinematica") is None


# -- failures (a stub lifecycle service) ------------------------------------------------------


class StubSessions:
    """What the watchdog uses of `SessionService`: the attached hook, `active` and `end`."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.hooks: list[Any] = []
        self.active: OpenSession | None = None
        self.ends: list[dict[str, Any]] = []

    def add_on_attached(self, hook: Any) -> None:
        self.hooks.append(hook)

    def attach(self, session_id: str) -> None:
        from datetime import UTC, datetime

        self.active = OpenSession(session_id, "fisica", "cinematica", datetime.now(UTC))
        for hook in self.hooks:
            hook(session_id)

    async def end(self, session_id: str, **kwargs: Any) -> None:
        self.ends.append({"session_id": session_id, **kwargs})
        if self.error is not None:
            raise self.error
        self.active = None


@pytest.mark.parametrize(
    ("error", "grace_restarts"),
    [(SessionAlreadyEndedError("ended meanwhile"), False), (RuntimeError("vault down"), True)],
)
def test_a_failed_idle_end_is_logged_and_never_raised(
    error: Exception, grace_restarts: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="studentassistant.server.capture_liveness")
    stub = StubSessions(error)
    clock = FakeClock()
    liveness = CaptureLiveness(stub, grace_seconds=GRACE, clock=clock)  # type: ignore[arg-type]
    stub.attach("20260927-100000")
    clock.advance(GRACE)

    assert anyio.run(liveness.tick) is None

    [call] = stub.ends
    assert call["reason"] == "idle" and call["idle_seconds"] == GRACE
    assert not liveness.ended_idle("20260927-100000")
    assert "20260927-100000" in caplog.text
    if grace_restarts:
        # Still active: tried again only after another full grace period.
        assert liveness.idle_seconds("20260927-100000") == 0.0
        assert anyio.run(liveness.tick) is None
        assert len(stub.ends) == 1
    else:
        assert liveness.idle_seconds("20260927-100000") is None


def test_the_grace_period_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CaptureLiveness(StubSessions(None), grace_seconds=0)  # type: ignore[arg-type]
