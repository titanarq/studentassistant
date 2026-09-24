"""Client STT mode: segments in, de-duplicated, published in session time and echoed back."""

from __future__ import annotations

from typing import Any

from ws_harness import WsHarness

from studentassistant.protocol import AudioFrame, encode_frame
from studentassistant.server.ws import (
    BUTTON,
    CLOSE_PROTOCOL_VIOLATION,
    COMMAND_ACK,
    MARKER,
    TRANSCRIPT_FINAL,
)

HELLO_AT = 1_000_000
# `ws` sets the backend clock 10 s after the session started, so with `hello` read at HELLO_AT
# the client clock showed CLIENT_T0 when the session started: session time = client - CLIENT_T0.
CLIENT_T0 = HELLO_AT - 10_000


def segment(
    segment_id: str, start_ms: int, end_ms: int, text: str, *, final: bool = True
) -> dict[str, Any]:
    """A client segment spanning session ms `start_ms`..`end_ms`."""
    return {
        "type": "transcript.client.final" if final else "transcript.client.partial",
        "segment_id": segment_id,
        "client_start_ms": CLIENT_T0 + start_ms,
        "client_end_ms": CLIENT_T0 + end_ms,
        "text": text,
        "provider": "web-speech",
        "language": "es-ES",
        "confidence": 0.9,
    }


def open_socket(ws: WsHarness, stack: Any) -> Any:
    socket = stack.enter_context(ws.connect())
    socket.send_json(ws.hello(HELLO_AT))
    assert socket.receive_json()["type"] == "hello.ack"
    return socket


def finals(ws: WsHarness) -> list[dict[str, Any]]:
    return [dict(event.payload) for event in ws.events_of(TRANSCRIPT_FINAL)]


def test_segments_are_ingested_published_and_echoed(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json(segment("seg-1", 2_000, 3_000, "la veloc", final=False))
        partial = socket.receive_json()
        socket.send_json(segment("seg-1", 2_000, 3_500, "la velocidad media"))
        final = socket.receive_json()

    assert partial == {
        "type": "transcript.partial",
        "segment_id": "seg-1",
        "session_start_ms": 2_000,
        "session_end_ms": 3_000,
        "text": "la veloc",
        "language": "es-ES",
        "confidence": 0.9,
    }
    assert final["type"] == "transcript.final"
    assert (final["session_start_ms"], final["session_end_ms"]) == (2_000, 3_500)

    [sink] = ws.sinks
    assert [(s.is_final, s.text) for s in sink.ingested] == [
        (False, "la veloc"),
        (True, "la velocidad media"),
    ]
    # Only the final is stored; the partial was a notice.
    [event] = ws.events_of(TRANSCRIPT_FINAL)
    assert (event.origin, event.t) == ("stt", 2_000)
    assert event.payload == {
        "segment_id": "seg-1",
        "session_start_ms": 2_000,
        "session_end_ms": 3_500,
        "text": "la velocidad media",
        "language": "es-ES",
        "provider": "web-speech",
        "confidence": 0.9,
    }
    assert "transcript.partial" not in {event.kind for event in ws.events()}


def test_a_repeated_final_and_a_late_partial_are_dropped(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json(segment("seg-1", 0, 1_000, "uno"))
        socket.send_json(segment("seg-1", 0, 1_000, "uno"))
        socket.send_json(segment("seg-1", 0, 900, "un", final=False))
        socket.send_json(segment("seg-2", 1_000, 2_000, "dos"))
        echoed = [socket.receive_json(), socket.receive_json()]

    assert [(m["type"], m["segment_id"]) for m in echoed] == [
        ("transcript.final", "seg-1"),
        ("transcript.final", "seg-2"),
    ]
    assert [p["segment_id"] for p in finals(ws)] == ["seg-1", "seg-2"]
    assert len(ws.sinks[0].ingested) == 2


def test_a_resend_after_reconnect_publishes_nothing_twice(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json(segment("seg-1", 0, 1_000, "uno"))
        socket.receive_json()

    # The client reconnects (its clock drifted: another offset) and resends what it is unsure of.
    ws.clock.now += 5_000
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT + 5_000))
        socket.receive_json()
        socket.send_json(segment("seg-1", 0, 1_000, "uno"))
        socket.send_json(segment("seg-2", 1_000, 2_000, "dos"))
        assert socket.receive_json()["segment_id"] == "seg-2"

    assert [p["segment_id"] for p in finals(ws)] == ["seg-1", "seg-2"]
    assert finals(ws)[1]["session_start_ms"] == 1_000


def test_button_marker_and_ack_are_published_with_mapped_times(ws: WsHarness) -> None:
    offset = ws.clock.now - HELLO_AT
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json(
            {
                "type": "button",
                "button": "switch_source",
                "source": "book",
                "client_time_ms": CLIENT_T0 + 4_000,
            }
        )
        socket.send_json(
            {"type": "marker", "label": "repasar", "client_time_ms": CLIENT_T0 + 5_000}
        )
        socket.send_json(
            {"type": "ack", "command_id": "cmd-1", "client_time_ms": CLIENT_T0 + 6_000}
        )
        # Messages are handled in order: once this final is echoed, the three above are stored.
        socket.send_json(segment("seg-1", 7_000, 8_000, "fin"))
        socket.receive_json()

    [button] = ws.events_of(BUTTON)
    [marker] = ws.events_of(MARKER)
    [ack] = ws.events_of(COMMAND_ACK)
    assert (button.origin, button.t) == ("phone", 4_000)
    assert button.payload == {
        "button": "switch_source",
        "source": "book",
        "client_time_ms": CLIENT_T0 + 4_000,
        "backend_time_ms": CLIENT_T0 + 4_000 + offset,
    }
    assert button.payload["backend_time_ms"] == ws.started_at_ms + 4_000
    assert (marker.t, marker.payload["label"]) == (5_000, "repasar")
    assert (ack.t, ack.payload["command_id"]) == (6_000, "cmd-1")


def test_an_invalid_message_is_refused_and_not_published(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json(segment("seg-1", 2_000, 1_000, "al revés"))
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert "transcript.client.final" in reason
    assert ws.events_of(TRANSCRIPT_FINAL) == []
    assert ws.sinks[0].ingested == []


def test_an_unknown_type_after_hello_is_refused(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_json({"type": "ping"})
        assert ws.receive_close(socket)[0] == CLOSE_PROTOCOL_VIOLATION


def test_audio_is_refused_in_client_mode(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        socket.receive_json()
        socket.send_bytes(encode_frame(AudioFrame(seq=0, client_time_ms=HELLO_AT, pcm=b"\0\0")))
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert "server stt_mode" in reason
