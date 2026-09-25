"""Ending a session flushes the gateway's server-side STT provider before `session.ended` (#136).

The TestClient is entered (`with ws.client`) so the app's lifespan runs the transcript pipeline
and every socket and request shares its event loop.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import AudioFrame, encode_frame
from studentassistant.server.ws import CLOSE_UNKNOWN_SESSION, TRANSCRIPT_FINAL
from studentassistant.stt import NormalisedSegment, SpeechToTextProvider
from studentassistant.stt.fakes import FakeProvider
from studentassistant.vault import TranscriptSegment, read_jsonl

HELLO_AT = 1_000_000
CLIENT_T0 = HELLO_AT - 10_000  # the client clock at session start (see `ws`)
FRAME_MS = 100
PCM = b"\0\0" * (16 * FRAME_MS)  # 100 ms of PCM16 16 kHz mono
RECEIVE_TIMEOUT_S = 5.0
# Two frames (200 ms of audio) reach only the first final; the rest is the provider's tail.
SCRIPT = [
    {"start": 0.0, "end": 0.15, "text": "la aceleración"},
    {"start": 0.3, "end": 0.9, "text": "es constante"},
    {"start": 1.0, "end": 1.4, "text": "en el", "is_final": False},
]


@pytest.fixture
def stt_settings() -> SttSettings:
    return SttSettings(
        mode="server", provider="fake", language="es", options={"fake": {"segments": SCRIPT}}
    )


def frame(seq: int) -> bytes:
    return encode_frame(AudioFrame(seq=seq, client_time_ms=CLIENT_T0 + seq * FRAME_MS, pcm=PCM))


def receive(socket: Any, timeout: float = RECEIVE_TIMEOUT_S) -> dict[str, Any]:
    """The next raw message the backend sent `socket`, failing the test after `timeout`.

    Starlette's `WebSocketTestSession.receive` waits forever; this reads the same in-portal stream
    under `anyio.fail_after` so a missing message fails fast.
    """

    async def read() -> Any:
        with anyio.fail_after(timeout):
            return await socket._send_rx.receive()

    try:
        return socket.portal.call(read)
    except TimeoutError:
        pytest.fail(f"no server message within {timeout} s")


def receive_json(socket: Any) -> dict[str, Any]:
    message = receive(socket)
    assert message["type"] == "websocket.send", message
    return json.loads(message["text"])


def stream(socket: Any, frames: int) -> list[dict[str, Any]]:
    """Handshake, send `frames` audio frames; the transcript messages received meanwhile."""
    socket.send_json(WsHarness.hello(HELLO_AT))
    assert receive_json(socket)["stt_mode"] == "server"
    transcripts = []
    for seq in range(frames):
        socket.send_bytes(frame(seq))
        while (message := receive_json(socket))["type"] != "ack":
            transcripts.append(message)
    return transcripts


def end_session(ws: WsHarness) -> None:
    response = ws.client.post(
        f"/api/sessions/{ws.session_id}/end", json={"client_time_ms": 90_000, "reason": "button"}
    )
    assert response.status_code == 200


def transcript(ws: WsHarness) -> list[str]:
    path = (
        ws.vault.path
        / "subjects/fisica/topics/cinematica/sessions"
        / ws.session_id
        / "transcript.jsonl"
    )
    return [s.text for s in read_jsonl(path, TranscriptSegment)]


def test_the_provider_tail_is_published_and_stored_before_session_ended(ws: WsHarness) -> None:
    with ws.client, ws.connect() as socket:
        transcripts = stream(socket, 2)
        end_session(ws)
        # The still-open socket is sent the tail, like any other transcript (the forwarder may
        # send the first final after its frame's ack, so count them all).
        while len(transcripts) < 3:
            transcripts.append(receive_json(socket))
        # ... and handles nothing more: the session is over.
        socket.send_bytes(frame(2))
        closed = receive(socket)

    assert [(m["type"], m["segment_id"], m["text"]) for m in transcripts] == [
        ("transcript.final", "server-1", "la aceleración"),
        ("transcript.final", "server-2", "es constante"),
        ("transcript.partial", "server-3", "en el"),
    ]
    assert closed["type"] == "websocket.close"
    assert closed["code"] == CLOSE_UNKNOWN_SESSION

    [provider] = ws.providers
    assert isinstance(provider, FakeProvider) and provider.finished
    kinds = [(e.kind, e.payload.get("text")) for e in ws.events()]
    ended = kinds.index(("session.ended", None))
    assert kinds.index((TRANSCRIPT_FINAL, "es constante")) < ended
    assert ws.events_of(TRANSCRIPT_FINAL)[-1].payload == {
        "segment_id": "server-2",
        "session_start_ms": 300,
        "session_end_ms": 900,
        "text": "es constante",
        "language": "es",
        "provider": "fake",
    }
    # The pipeline drained before the vault ended the session: the tail is in the transcript.
    assert transcript(ws) == ["la aceleración", "es constante"]
    # The session's receive state is gone.
    assert ws.app.state.gateway._states == {}


def test_the_tail_is_flushed_with_no_socket_open(ws: WsHarness) -> None:
    with ws.client:
        with ws.connect() as socket:
            stream(socket, 2)
        end_session(ws)
        # A socket of the ended session is refused.
        with ws.connect() as socket:
            closed = receive(socket)

    assert closed["code"] == CLOSE_UNKNOWN_SESSION
    assert transcript(ws) == ["la aceleración", "es constante"]
    assert ws.app.state.gateway._states == {}


class _FailingFinish(FakeProvider):
    async def finish(self) -> list[NormalisedSegment]:
        raise RuntimeError("the recogniser died")


def test_a_failing_flush_still_ends_the_session_and_drops_its_state(ws: WsHarness) -> None:
    def factory(settings: SttSettings) -> SpeechToTextProvider:
        return _FailingFinish(settings.options.get("fake"), language=settings.language)

    ws.app.state.gateway.provider_factory = factory
    with ws.client:
        with ws.connect() as socket:
            stream(socket, 2)
        end_session(ws)

    assert [e.kind for e in ws.events()][-1] == "session.ended"
    assert transcript(ws) == ["la aceleración"]
    assert ws.app.state.gateway._states == {}


def test_ending_a_session_that_never_streamed_is_a_no_op_for_the_gateway(ws: WsHarness) -> None:
    with ws.client:
        end_session(ws)
    assert ws.providers == []
    assert ws.events_of(TRANSCRIPT_FINAL) == []
    assert ws.app.state.gateway._states == {}
