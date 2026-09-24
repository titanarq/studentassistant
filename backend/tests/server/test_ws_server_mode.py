"""Server STT mode: audio frames in, de-duplicated and ordered by `seq`, fed to the provider."""

from __future__ import annotations

import struct
from typing import Any

import pytest
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import AudioFrame, encode_frame
from studentassistant.server.ws import (
    CLOSE_PROTOCOL_VIOLATION,
    MAX_PENDING_FRAMES,
    TRANSCRIPT_FINAL,
)
from studentassistant.stt.fakes import FakeProvider

HELLO_AT = 1_000_000
CLIENT_T0 = HELLO_AT - 10_000  # the client clock at session start (see `ws`)
FRAME_MS = 100
PCM = b"\0\0" * (16 * FRAME_MS)  # 100 ms of PCM16 16 kHz mono


@pytest.fixture
def stt_settings() -> SttSettings:
    script = [
        {"start": 0.0, "end": 0.15, "text": "la", "is_final": False},
        {"start": 0.0, "end": 0.25, "text": "la aceleración", "confidence": 0.8},
        {"start": 0.3, "end": 0.55, "text": "es constante"},
    ]
    return SttSettings(
        mode="server", provider="fake", language="es", options={"fake": {"segments": script}}
    )


def frame(seq: int) -> bytes:
    return encode_frame(AudioFrame(seq=seq, client_time_ms=CLIENT_T0 + seq * FRAME_MS, pcm=PCM))


class Client:
    """A connected server-mode client that keeps every transcript message it was sent."""

    def __init__(self, ws: WsHarness, socket: Any, hello_at: int = HELLO_AT) -> None:
        self.socket = socket
        self.transcripts: list[dict[str, Any]] = []
        self.acks: list[int] = []
        socket.send_json(ws.hello(hello_at))
        assert socket.receive_json()["stt_mode"] == "server"
        # A client whose session already received audio is told where it stands.
        self.resumed_at = ws.app.state.gateway.state_for(ws.session_id).last_contiguous_seq
        if self.resumed_at is not None:
            assert self._next() == {
                "type": "ack",
                "audio_seq": self.resumed_at,
                "server_time_ms": ws.clock.now,
            }

    def _next(self) -> dict[str, Any]:
        message = self.socket.receive_json()
        if message["type"] == "ack":
            self.acks.append(message["audio_seq"])
        else:
            self.transcripts.append(message)
        return message

    def send(self, seq: int) -> int:
        """Send frame `seq`; the `audio_seq` of the ack it gets."""
        self.socket.send_bytes(frame(seq))
        while self._next()["type"] != "ack":
            pass
        return self.acks[-1]

    def wait_for_transcripts(self, count: int) -> list[dict[str, Any]]:
        while len(self.transcripts) < count:
            self._next()
        return self.transcripts


def fed_starts(ws: WsHarness) -> list[float]:
    [provider] = ws.providers
    assert isinstance(provider, FakeProvider)
    return [round(chunk.start, 3) for chunk in provider.fed]


def test_frames_are_fed_acked_and_their_segments_published(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        assert [client.send(seq) for seq in range(6)] == [0, 1, 2, 3, 4, 5]
        transcripts = client.wait_for_transcripts(3)

    assert fed_starts(ws) == [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
    assert [(m["type"], m["segment_id"], m["text"]) for m in transcripts] == [
        ("transcript.partial", "server-1", "la"),
        ("transcript.final", "server-1", "la aceleración"),
        ("transcript.final", "server-2", "es constante"),
    ]
    first = ws.events_of(TRANSCRIPT_FINAL)[0]
    assert first.payload == {
        "segment_id": "server-1",
        "session_start_ms": 0,
        "session_end_ms": 250,
        "text": "la aceleración",
        "language": "es",
        "provider": "fake",
        "confidence": 0.8,
    }
    assert [e.payload["segment_id"] for e in ws.events_of(TRANSCRIPT_FINAL)] == [
        "server-1",
        "server-2",
    ]


def test_out_of_order_frames_are_fed_in_seq_order(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        assert [client.send(seq) for seq in (0, 2, 1, 3)] == [0, 0, 2, 3]
    assert fed_starts(ws) == [0.0, 0.1, 0.2, 0.3]


def test_repeated_frames_are_fed_once(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        assert [client.send(seq) for seq in (0, 1, 1, 0, 3, 3)] == [0, 1, 1, 1, 1, 1]
    assert fed_starts(ws) == [0.0, 0.1]


def test_a_reconnect_resending_from_the_ack_leaves_no_gap_or_duplicate(ws: WsHarness) -> None:
    with ws.connect() as socket:
        client = Client(ws, socket)
        for seq in range(3):
            client.send(seq)
        client.wait_for_transcripts(2)

    ws.clock.now += 2_000
    with ws.connect() as socket:
        client = Client(ws, socket, HELLO_AT + 2_000)
        assert client.resumed_at == 2
        assert [client.send(seq) for seq in range(2, 6)] == [2, 3, 4, 5]
        client.wait_for_transcripts(1)

    assert len(ws.providers) == 1  # the provider outlived the first socket
    assert fed_starts(ws) == [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
    assert [e.payload["segment_id"] for e in ws.events_of(TRANSCRIPT_FINAL)] == [
        "server-1",
        "server-2",
    ]


def test_a_frame_before_hello_ack_is_refused(ws: WsHarness) -> None:
    with ws.connect() as socket:
        socket.send_bytes(frame(0))
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert "hello.ack" in reason
    assert ws.providers == []


@pytest.mark.parametrize(
    "data",
    [
        b"XXXX" + frame(0)[4:],
        struct.pack(">4sBBIQ", b"SAAF", 2, 0, 0, CLIENT_T0) + PCM,
        b"SAAF",
    ],
    ids=["wrong-magic", "other-major", "short"],
)
def test_a_malformed_frame_is_refused(ws: WsHarness, data: bytes) -> None:
    with ws.connect() as socket:
        Client(ws, socket)
        socket.send_bytes(data)
        code, _ = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert fed_starts(ws) == []


def test_client_transcripts_are_refused_in_server_mode(ws: WsHarness) -> None:
    with ws.connect() as socket:
        Client(ws, socket)
        socket.send_json(
            {
                "type": "transcript.client.final",
                "segment_id": "seg-1",
                "client_start_ms": CLIENT_T0,
                "client_end_ms": CLIENT_T0 + 100,
                "text": "hola",
                "provider": "web-speech",
                "language": "es-ES",
            }
        )
        assert ws.receive_close(socket)[0] == CLOSE_PROTOCOL_VIOLATION
    assert ws.events_of(TRANSCRIPT_FINAL) == []


def test_a_frame_too_far_ahead_is_refused(ws: WsHarness) -> None:
    with ws.connect() as socket:
        Client(ws, socket)
        socket.send_bytes(frame(MAX_PENDING_FRAMES))
        code, reason = ws.receive_close(socket)
    assert code == CLOSE_PROTOCOL_VIOLATION
    assert "resend" in reason
