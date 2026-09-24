"""End to end: gateway -> bus -> `TranscriptPipeline` -> `transcript.jsonl`, in both STT modes.

The TestClient is entered (`with ws.client`) so the app's lifespan runs the pipeline and every
socket and request shares its event loop.
"""

from __future__ import annotations

from typing import Any

import pytest
from ws_harness import WsHarness

from studentassistant.config import SttSettings
from studentassistant.protocol import AudioFrame, encode_frame
from studentassistant.stt import (
    BufferedProvider,
    SpeechToTextProvider,
    TranscriptPipeline,
    buffered_provider_from_settings,
)
from studentassistant.stt.fakes import FakeProvider
from studentassistant.vault import TranscriptSegment, read_jsonl

HELLO_AT = 1_000_000
# `ws` sets the backend clock 10 s after the session started: session time = client - CLIENT_T0.
CLIENT_T0 = HELLO_AT - 10_000


def transcript(ws: WsHarness) -> list[tuple[int, int, str]]:
    path = (
        ws.vault.path
        / "subjects/fisica/topics/cinematica/sessions"
        / ws.session_id
        / "transcript.jsonl"
    )
    return [(s.t_start, s.t_end, s.text) for s in read_jsonl(path, TranscriptSegment)]


def end_session(ws: WsHarness) -> None:
    pipeline: TranscriptPipeline = ws.app.state.transcripts
    ws.client.portal.call(pipeline.drain)  # type: ignore[union-attr]
    response = ws.client.post(
        f"/api/sessions/{ws.session_id}/end", json={"client_time_ms": 90_000, "reason": "button"}
    )
    assert response.status_code == 200


def client_final(segment_id: str, start_ms: int, end_ms: int, text: str) -> dict[str, Any]:
    return {
        "type": "transcript.client.final",
        "segment_id": segment_id,
        "client_start_ms": CLIENT_T0 + start_ms,
        "client_end_ms": CLIENT_T0 + end_ms,
        "text": text,
        "provider": "web-speech",
        "language": "es-ES",
    }


def test_client_segments_become_the_transcript_once_each(ws: WsHarness) -> None:
    sent = [
        client_final("seg-1", 2_000, 3_500, "la velocidad media"),
        client_final("seg-1", 2_000, 3_500, "la velocidad media"),  # resent: gateway drops it
        client_final("seg-1b", 2_000, 3_500, "la velocidad media"),  # same words, new id
        client_final("seg-3", 5_000, 6_000, "es constante"),
        client_final("seg-2", 4_000, 4_800, "se mide en metros"),  # arrives out of order
        client_final("seg-4", 5_500, 8_000, "es constante por segundo"),  # restarted recognizer
    ]
    with ws.client, ws.connect() as socket:
        socket.send_json(ws.hello(HELLO_AT))
        assert socket.receive_json()["type"] == "hello.ack"
        for message in sent:
            socket.send_json(message)
        echoed = [socket.receive_json() for _ in range(len(sent) - 1)]
        assert all(m["type"] == "transcript.final" for m in echoed)
        end_session(ws)

    # Session times follow the handshake clock offset (client ms - CLIENT_T0).
    assert transcript(ws) == [
        (2_000, 3_500, "la velocidad media"),
        (5_000, 6_000, "es constante"),
        (5_000, 5_000, "se mide en metros"),
        (6_000, 8_000, "por segundo"),
    ]
    # The bus events stay as the gateway published them.
    assert len(ws.events_of("transcript.final")) == 5


FRAME_MS = 100
PCM = b"\0\0" * (16 * FRAME_MS)  # 100 ms of PCM16 16 kHz mono
SCRIPT = [
    {"start": 0.0, "end": 0.15, "text": "la", "is_final": False},
    {"start": 0.0, "end": 0.25, "text": "la aceleración"},
    {"start": 0.3, "end": 0.55, "text": "es constante"},
    {"start": 0.6, "end": 0.95, "text": "en el tiempo"},
]


def frame(seq: int) -> bytes:
    return encode_frame(AudioFrame(seq=seq, client_time_ms=CLIENT_T0 + seq * FRAME_MS, pcm=PCM))


class TestServerMode:
    @pytest.fixture
    def stt_settings(self) -> SttSettings:
        return SttSettings(
            mode="server", provider="fake", language="es", options={"fake": {"segments": SCRIPT}}
        )

    def test_audio_becomes_the_transcript(self, ws: WsHarness) -> None:
        providers: list[SpeechToTextProvider] = []

        def factory(settings: SttSettings) -> SpeechToTextProvider:
            provider = buffered_provider_from_settings(settings)
            providers.append(provider)
            return provider

        ws.app.state.gateway.provider_factory = factory
        finals: list[dict[str, Any]] = []
        with ws.client, ws.connect() as socket:
            socket.send_json(ws.hello(HELLO_AT))
            assert socket.receive_json()["stt_mode"] == "server"
            seq = 0
            # Keep streaming (silence) until every scripted final has come back: the buffered
            # provider hands out what the fake produced on a later frame, never blocking one.
            while len(finals) < 3:
                assert seq < 200, "the scripted finals never came back"
                socket.send_bytes(frame(seq))
                seq += 1
                while (message := socket.receive_json())["type"] != "ack":
                    if message["type"] == "transcript.final":
                        finals.append(message)
            end_session(ws)

        [provider] = providers
        assert isinstance(provider, BufferedProvider)
        assert isinstance(provider.inner, FakeProvider)
        assert transcript(ws) == [
            (0, 250, "la aceleración"),
            (300, 550, "es constante"),
            (600, 950, "en el tiempo"),
        ]
