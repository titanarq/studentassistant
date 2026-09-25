"""End-to-end replay (`server/replay.py`): the sample recording, through `create_app`, into a vault.

The replay drives the in-process app through ASGI exactly as a capture client would (REST start,
`hello`, the timed transcript and button, the capture upload, the end), on a virtual clock that
the injected `sleep` advances, so nothing here waits in real time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI

from studentassistant.config import ServerSettings, SttSettings
from studentassistant.protocol import TranscriptClientFinal
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import Recording, read_recording
from studentassistant.server.replay import (
    AsgiTransport,
    ReplayError,
    ReplayResult,
    replay,
    timeline,
)
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
