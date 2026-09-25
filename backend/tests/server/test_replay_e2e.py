"""End-to-end replay (`server/replay.py`): the sample recording, through `create_app`, into a vault.

A server-mode recording (a generated `audio.wav`) is replayed as binary audio frames into the fake
STT provider injected through the gateway's `provider_factory`, also across a dropped socket.

The replay drives the in-process app through ASGI exactly as a capture client would (REST start,
`hello`, the timed transcript and button, the capture upload, the end), on a virtual clock that
the injected `sleep` advances, so nothing here waits in real time.
"""

from __future__ import annotations

import asyncio
import math
import struct
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from studentassistant.config import ServerSettings, SttSettings
from studentassistant.protocol import TranscriptClientFinal
from studentassistant.protocol.audio import SAMPLE_RATE_HZ
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import (
    Recording,
    RecordingManifest,
    RecordingWriter,
    read_recording,
)
from studentassistant.server.replay import (
    AUDIO_FRAME_MS,
    AsgiTransport,
    ReplayError,
    ReplayResult,
    ReplaySocket,
    ReplayTransport,
    Response,
    replay,
    timeline,
)
from studentassistant.stt import SpeechToTextProvider
from studentassistant.stt.fakes import FakeProvider
from studentassistant.vault import Event, Vault, list_sessions, read_jsonl, sources_directory

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"


class VirtualTime:
    """A monotonic clock that only `sleep` moves, and the delays it was asked to wait."""

    def __init__(self) -> None:
        self.now = 100.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds


@pytest.fixture
def recording() -> Recording:
    return read_recording(FIXTURE)


@pytest.fixture
def replay_app(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        stt=SttSettings(mode="client", provider="web-speech", language="es"),
    )


def run_replay(
    app: FastAPI, recording: Recording, time: VirtualTime, **options: object
) -> ReplayResult:
    async def main() -> ReplayResult:
        async with AsgiTransport(app) as transport:
            return await replay(recording, transport, sleep=time.sleep, clock=time.clock, **options)

    return asyncio.run(main())


def session_events(vault: Vault, subject: str, topic: str, session_id: str) -> list[Event]:
    path = vault.path / "subjects" / subject / "topics" / topic / "sessions" / session_id
    return list(read_jsonl(path / "events.jsonl", Event))


def test_the_sample_replays_into_the_vault(
    replay_app: FastAPI, recording: Recording, tmp_vault: Vault
) -> None:
    time = VirtualTime()

    result = run_replay(replay_app, recording, time, speed=4)

    finals = [m.text for m in recording.transcript if isinstance(m, TranscriptClientFinal)]
    assert (result.subject_id, result.topic_id) == ("biologia", "la-celula")
    assert (result.finals_sent, result.events_sent, result.captures_stored) == (3, 1, 1)
    events = session_events(tmp_vault, "biologia", "la-celula", result.session_id)
    assert [e.payload["text"] for e in events if e.kind == "transcript.final"] == finals
    # Session times keep the recorded offsets whatever the replay speed; they all shift by the
    # real time the backend took between starting the session and receiving `hello`.
    shift = min(e.t for e in events if e.kind == "transcript.final") - 500
    assert 0 <= shift < 1000
    assert [e.t - shift for e in events if e.kind == "transcript.final"] == [500, 4000, 11000]
    [button] = [e for e in events if e.kind == "button"]
    assert (button.payload["button"], button.payload["source"], button.t - shift) == (
        "switch_source",
        "book",
        8000,
    )

    # The capture, uploaded after the `switch_source` button, is a `book` source of the topic.
    [capture] = recording.captures
    [stored] = [e for e in events if e.kind == "capture.stored"]
    assert stored.payload["capture_id"] == capture.metadata.capture_id
    assert stored.payload["source_context"] == "book"
    book = sources_directory(tmp_vault, "biologia", "la-celula", "book")
    assert (book / "page-001.jpg").read_bytes() == capture.image_paths[0].read_bytes()
    sidecar = yaml.safe_load((book / "page-001.yaml").read_text(encoding="utf-8"))
    assert sidecar["capture_id"] == capture.metadata.capture_id
    assert not sources_directory(tmp_vault, "biologia", "la-celula", "notes").exists()

    [meta] = list_sessions(tmp_vault, "biologia", "la-celula")
    assert meta.id == result.session_id
    assert meta.ended_at is not None


def test_pacing_waits_each_recorded_offset_divided_by_the_speed(
    replay_app: FastAPI, recording: Recording
) -> None:
    time = VirtualTime()

    run_replay(replay_app, recording, time, speed=4)

    start = recording.manifest.started_client_time_ms
    last = timeline(recording)[-1].client_time_ms
    assert time.delays and all(delay > 0 for delay in time.delays)
    assert sum(time.delays) == pytest.approx((last - start) / 1000 / 4)


def test_the_topic_override_replays_into_another_topic(
    replay_app: FastAPI, recording: Recording, tmp_vault: Vault
) -> None:
    result = run_replay(
        replay_app, recording, VirtualTime(), speed=10, subject="fisica", topic="cinematica"
    )

    assert (result.subject_id, result.topic_id) == ("fisica", "cinematica")
    events = session_events(tmp_vault, "fisica", "cinematica", result.session_id)
    assert len([e for e in events if e.kind == "transcript.final"]) == 3
    assert not (tmp_vault.path / "subjects" / "biologia").exists()


def test_a_refused_start_is_a_replay_error(
    replay_app: FastAPI, recording: Recording, tmp_vault: Vault
) -> None:
    async def main() -> None:
        async with AsgiTransport(replay_app) as transport:
            # The first replay's session is left open by starting a second one on top of it.
            first = await transport.request(
                "POST", "/api/subjects", b'{"name": "biologia"}', "application/json"
            )
            assert first.status == 201
            topic = await transport.request(
                "POST",
                "/api/subjects/biologia/topics",
                b'{"name": "la-celula"}',
                "application/json",
            )
            assert topic.status == 201
            started = await transport.request(
                "POST",
                "/api/sessions",
                b'{"subject_id": "biologia", "topic_id": "la-celula", "client_time_ms": 1}',
                "application/json",
            )
            assert started.status == 201
            time = VirtualTime()
            await replay(recording, transport, sleep=time.sleep, clock=time.clock)

    with pytest.raises(ReplayError, match="409"):
        asyncio.run(main())


# -- server mode: audio frames into the fake STT provider -------------------------------------

AUDIO_SECONDS = 3
HEARD = [
    {"start": 0.2, "end": 1.0, "text": "La mitocondria produce la energía de la célula."},
    {"start": 1.2, "end": 2.4, "text": "El núcleo guarda el ADN."},
]


def tone(seconds: float) -> bytes:
    """A 440 Hz PCM16 tone: every sample differs from its neighbours, so a gap or a repeat in
    what the provider was fed shows up when it is compared with the file."""
    count = int(seconds * SAMPLE_RATE_HZ)
    return struct.pack(
        f"<{count}h",
        *(int(12000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE_HZ)) for i in range(count)),
    )


@pytest.fixture
def audio_recording(tmp_path: Path) -> Recording:
    manifest = RecordingManifest(
        format_version=1,
        subject="biologia",
        topic="la-celula",
        language="es",
        stt_mode="server",
        started_client_time_ms=1_750_000_000_000,
    )
    with RecordingWriter(tmp_path / "audio-recording", manifest) as writer:
        writer.append_audio(tone(AUDIO_SECONDS))
    return read_recording(tmp_path / "audio-recording")


@pytest.fixture
def providers() -> list[FakeProvider]:
    return []


@pytest.fixture
def audio_app(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    providers: list[FakeProvider],
) -> FastAPI:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        stt=SttSettings(mode="server", provider="fake", language="es"),
    )

    def provider_factory(settings: SttSettings) -> SpeechToTextProvider:
        provider = FakeProvider(language=settings.language, segments=HEARD)
        providers.append(provider)
        return provider

    app.state.gateway.provider_factory = provider_factory
    return app


class DroppingSocket:
    """A socket that drops (closes, losing whatever is sent next) after `drop_after` frames."""

    def __init__(self, inner: ReplaySocket, drop_after: int) -> None:
        self.inner = inner
        self.drop_after = drop_after
        self.frames = 0
        self.dropped = False

    def __str__(self) -> str:
        return str(self.inner)

    async def send_text(self, text: str) -> None:
        if not self.dropped:
            await self.inner.send_text(text)

    async def send_bytes(self, data: bytes) -> None:
        if self.dropped:
            return
        self.frames += 1
        if self.frames > self.drop_after:
            self.dropped = True
            await self.inner.close()
            return
        await self.inner.send_bytes(data)

    async def receive_text(self) -> str | None:
        return await self.inner.receive_text()

    async def settle(self) -> None:
        await self.inner.settle()

    async def close(self) -> None:
        await self.inner.close()


class DroppingTransport:
    """Wraps a transport so that only its first session socket drops."""

    def __init__(self, inner: ReplayTransport, drop_after: int) -> None:
        self.inner = inner
        self.drop_after = drop_after
        self.connections = 0

    async def request(self, *args: Any, **kwargs: Any) -> Response:
        return await self.inner.request(*args, **kwargs)

    async def connect(self, path: str) -> ReplaySocket:
        self.connections += 1
        socket = await self.inner.connect(path)
        return DroppingSocket(socket, self.drop_after) if self.connections == 1 else socket


def run_audio_replay(
    app: FastAPI, recording: Recording, *, drop_after: int | None = None
) -> tuple[ReplayResult, int]:
    time = VirtualTime()

    async def main() -> tuple[ReplayResult, int]:
        async with AsgiTransport(app) as asgi:
            if drop_after is None:
                result = await replay(recording, asgi, speed=4, sleep=time.sleep, clock=time.clock)
                return result, 1
            transport = DroppingTransport(asgi, drop_after)
            result = await replay(recording, transport, speed=4, sleep=time.sleep, clock=time.clock)
            return result, transport.connections

    return asyncio.run(main())


def assert_audio_fed_once(recording: Recording, providers: list[FakeProvider]) -> None:
    """One provider for the session, fed every frame exactly once, in order, gapless."""
    [provider] = providers
    frames = AUDIO_SECONDS * 1000 // AUDIO_FRAME_MS
    assert len(provider.fed) == frames
    assert b"".join(chunk.data for chunk in provider.fed) == recording.read_audio()
    starts = [chunk.start for chunk in provider.fed]
    assert starts == sorted(starts)


def test_a_server_mode_recording_streams_its_audio_into_the_provider(
    audio_app: FastAPI,
    audio_recording: Recording,
    providers: list[FakeProvider],
    tmp_vault: Vault,
) -> None:
    result, connections = run_audio_replay(audio_app, audio_recording)

    frames = AUDIO_SECONDS * 1000 // AUDIO_FRAME_MS
    assert (result.audio_frames_sent, result.audio_frames_resent, result.reconnects) == (
        frames,
        0,
        0,
    )
    assert (result.finals_sent, result.partials_sent) == (0, 0)
    assert_audio_fed_once(audio_recording, providers)
    events = session_events(tmp_vault, "biologia", "la-celula", result.session_id)
    assert [e.payload["text"] for e in events if e.kind == "transcript.final"] == [
        segment["text"] for segment in HEARD
    ]
    [meta] = list_sessions(tmp_vault, "biologia", "la-celula")
    assert meta.ended_at is not None


def test_a_dropped_socket_resends_the_audio_from_the_last_ack(
    audio_app: FastAPI,
    audio_recording: Recording,
    providers: list[FakeProvider],
    tmp_vault: Vault,
) -> None:
    result, connections = run_audio_replay(audio_app, audio_recording, drop_after=12)

    assert (connections, result.reconnects) == (2, 1)
    # Frames 0..11 reached the backend and at most those were acknowledged; everything from the
    # last ack on was sent again on the second socket.
    assert result.audio_frames_resent >= result.audio_frames_sent - 12
    assert result.audio_frames_resent > 0
    assert_audio_fed_once(audio_recording, providers)
    events = session_events(tmp_vault, "biologia", "la-celula", result.session_id)
    assert [e.payload["text"] for e in events if e.kind == "transcript.final"] == [
        segment["text"] for segment in HEARD
    ]
    [meta] = list_sessions(tmp_vault, "biologia", "la-celula")
    assert meta.ended_at is not None


def test_a_socket_that_keeps_dropping_is_a_replay_error(
    audio_app: FastAPI, audio_recording: Recording
) -> None:
    class AlwaysDropping(DroppingTransport):
        async def connect(self, path: str) -> ReplaySocket:
            self.connections += 1
            return DroppingSocket(await self.inner.connect(path), self.drop_after)

    async def main() -> None:
        async with AsgiTransport(audio_app) as asgi:
            time = VirtualTime()
            await replay(
                audio_recording,
                AlwaysDropping(asgi, drop_after=0),
                sleep=time.sleep,
                clock=time.clock,
            )

    with pytest.raises(ReplayError, match="dropped"):
        asyncio.run(main())
