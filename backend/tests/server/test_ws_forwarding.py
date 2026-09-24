"""Bus -> client: transcript, command, notice and capture events of the session reach the socket."""

from __future__ import annotations

import functools
from typing import Any

from ws_harness import WsHarness

from studentassistant.protocol import parse_server_event
from studentassistant.server.bus import SessionBus


def publish(socket: Any, bus: SessionBus, *args: Any, **kwargs: Any) -> None:
    """Publish on the bus from the test thread, in the loop serving `socket`."""
    socket.portal.call(functools.partial(bus.publish, *args, **kwargs))


def connected(ws: WsHarness, socket: Any) -> SessionBus:
    socket.send_json(ws.hello())
    assert socket.receive_json()["type"] == "hello.ack"
    return ws.app.state.bus


def test_command_and_notice_are_forwarded_as_server_messages(ws: WsHarness) -> None:
    with ws.connect() as socket:
        bus = connected(ws, socket)
        publish(
            socket,
            bus,
            ws.session_id,
            "command",
            "stt",
            {"command_id": "cmd-1", "command": "capture_now"},
        )
        publish(
            socket, bus, ws.session_id, "notice", "observer", {"pending_count": 3}, persist=False
        )
        command = socket.receive_json()
        notice = socket.receive_json()

    parse_server_event(command)
    parse_server_event(notice)
    assert command == {
        "type": "command",
        "command_id": "cmd-1",
        "command": "capture_now",
        "server_time_ms": ws.clock.now,
    }
    assert notice == {"type": "notice", "pending_count": 3, "server_time_ms": ws.clock.now}


def test_transcript_events_from_elsewhere_are_forwarded(ws: WsHarness) -> None:
    payload = {
        "segment_id": "seg-9",
        "session_start_ms": 100,
        "session_end_ms": 200,
        "text": "hola",
        "language": "es",
        "provider": "fake",
    }
    with ws.connect() as socket:
        bus = connected(ws, socket)
        publish(socket, bus, ws.session_id, "transcript.partial", "stt", payload, persist=False)
        message = socket.receive_json()
    assert parse_server_event(message).type == "transcript.partial"
    assert message == {
        "type": "transcript.partial",
        **{k: v for k, v in payload.items() if k != "provider"},
    }


def test_other_kinds_and_unusable_payloads_are_not_forwarded(ws: WsHarness) -> None:
    with ws.connect() as socket:
        bus = connected(ws, socket)
        publish(socket, bus, ws.session_id, "observer.state_op", "observer", {"op": "note"})
        publish(socket, bus, ws.session_id, "command", "stt", {"command": "capture_now"})
        publish(
            socket, bus, ws.session_id, "notice", "observer", {"pending_count": -1}, persist=False
        )
        # A capture ack needs a persisted `capture.stored` naming a valid `capture_id`.
        publish(socket, bus, ws.session_id, "capture.stored", "phone", {"trigger": "button"})
        publish(
            socket,
            bus,
            ws.session_id,
            "capture.stored",
            "phone",
            {"capture_id": "0b6f3c2e-9a41-4d8e-8f7a-2c5d1e3b4a60"},
            persist=False,
        )
        publish(
            socket, bus, ws.session_id, "notice", "observer", {"pending_count": 1}, persist=False
        )
        assert socket.receive_json()["pending_count"] == 1


def test_the_subscription_is_released_on_disconnect(ws: WsHarness) -> None:
    bus: SessionBus = ws.app.state.bus
    before = len(bus.subscriptions)
    with ws.connect() as socket:
        connected(ws, socket)
        [mine] = [s for s in bus.subscriptions if s.name == f"ws:{ws.session_id}"]
        assert mine.session_id == ws.session_id
        assert len(bus.subscriptions) == before + 1
    assert mine.closed
    assert len(bus.subscriptions) == before


def test_the_subscription_is_released_when_the_socket_is_refused(ws: WsHarness) -> None:
    bus: SessionBus = ws.app.state.bus
    before = len(bus.subscriptions)
    with ws.connect() as socket:
        connected(ws, socket)
        socket.send_json({"type": "nope"})
        ws.receive_close(socket)
    assert len(bus.subscriptions) == before
