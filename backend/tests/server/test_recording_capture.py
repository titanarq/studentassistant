"""`serve --record`: the recorder behind the gateway and the capture upload (`server/recorder.py`).

A session driven through an app built with a `SessionRecorder` over a temporary directory (the
wiring `serve --record` does) is recorded there, and nowhere under the vault; replaying that
recording into a fresh vault gives the same transcript finals and the same captures. The session
is driven by `replay` itself (the sample fixture, over ASGI, on a virtual clock), so what arrives
at the gateway is what a capture client sends; a direct WebSocket test covers the resends a
recording must not duplicate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from test_replay_e2e import HEARD, VirtualTime, session_events, tone
from typer.testing import CliRunner
from ws_harness import WsHarness

from studentassistant.cli import cli
from studentassistant.config import ServerSettings, SttSettings
from studentassistant.protocol import TranscriptClientFinal, TranscriptClientPartial
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recorder import SessionRecorder
from studentassistant.server.recording import (
    Recording,
    RecordingManifest,
    RecordingWriter,
    read_recording,
)
from studentassistant.server.replay import AsgiTransport, ReplayResult, replay
from studentassistant.stt import SpeechToTextProvider
from studentassistant.stt.fakes import FakeProvider
from studentassistant.vault import Vault, sources_directory

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
CLIENT_STT = SttSettings(mode="client", provider="web-speech", language="es")
SERVER_STT = SttSettings(mode="server", provider="fake", language="es")


def make_app(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    vault: Vault,
    stt: SttSettings,
    recorder: SessionRecorder | None = None,
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=vault,
        stt=stt,
        recorder=recorder,
    )


def run_replay(app: FastAPI, recording: Recording) -> ReplayResult:
    time = VirtualTime()

    async def main() -> ReplayResult:
        async with AsgiTransport(app) as transport:
            return await replay(recording, transport, speed=4, sleep=time.sleep, clock=time.clock)

    return asyncio.run(main())


def finals(recording: Recording) -> list[str]:
    return [m.text for m in recording.transcript if isinstance(m, TranscriptClientFinal)]


def vault_finals(vault: Vault, result: ReplayResult) -> list[str]:
    events = session_events(vault, result.subject_id, result.topic_id, result.session_id)
    return [e.payload["text"] for e in events if e.kind == "transcript.final"]


def vault_files(vault: Vault) -> set[Path]:
    return {
        path.relative_to(vault.path)
        for path in vault.path.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(vault.path).parts
    }


@pytest.fixture
def fresh_vault(tmp_path: Path, tmp_vault: Vault) -> Vault:
    """A second vault, next to `tmp_vault` (which already moved `HOME` into `tmp_path`)."""
    return Vault.init(tmp_path / "fresh-vault", student="Ana García")


def test_a_recorded_session_replays_into_a_fresh_vault_with_the_same_finals_and_captures(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    fresh_vault: Vault,
) -> None:
    recordings = tmp_path / "recordings"
    live = make_app(server, codes, tmp_path, tmp_vault, CLIENT_STT, SessionRecorder(recordings))
    sample = read_recording(FIXTURE)

    live_result = run_replay(live, sample)

    [directory] = list(recordings.iterdir())
    assert directory.name == live_result.session_id
    recorded = read_recording(directory)
    assert (recorded.manifest.subject, recorded.manifest.topic) == ("biologia", "la-celula")
    assert (recorded.manifest.stt_mode, recorded.manifest.stt_provider) == ("client", "web-speech")
    assert recorded.audio_path is None
    # Exactly what the client sent: every partial and final, the button, the burst's images.
    assert recorded.transcript == sample.transcript
    assert recorded.events == sample.events
    [capture] = recorded.captures
    [original] = sample.captures
    assert capture.metadata == original.metadata
    assert capture.image_paths[0].read_bytes() == original.image_paths[0].read_bytes()
    # Client times keep their offsets from the session start (the client clock is the sample's).
    start = recorded.manifest.started_client_time_ms
    assert abs(sample.manifest.started_client_time_ms - start) < 1000

    # Nothing of the recording is under the vault root.
    assert not recordings.resolve().is_relative_to(tmp_vault.path.resolve())
    live_files = vault_files(tmp_vault)
    assert not [p for p in live_files if p.name in ("manifest.yaml", "captures.jsonl")]
    assert not [p for p in live_files if p.parts[:1] == ("recordings",)]

    fresh = make_app(server, codes, tmp_path, fresh_vault, CLIENT_STT)
    fresh_result = run_replay(fresh, recorded)

    assert vault_finals(fresh_vault, fresh_result) == finals(sample)
    assert vault_finals(fresh_vault, fresh_result) == vault_finals(tmp_vault, live_result)
    assert fresh_result.captures_stored == live_result.captures_stored == 1
    live_book = sources_directory(tmp_vault, "biologia", "la-celula", "book")
    fresh_book = sources_directory(fresh_vault, "biologia", "la-celula", "book")
    assert (fresh_book / "page-001.jpg").read_bytes() == (live_book / "page-001.jpg").read_bytes()


def test_a_recorded_server_mode_session_keeps_its_audio(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    fresh_vault: Vault,
) -> None:
    source_dir = tmp_path / "audio-source"
    manifest = RecordingManifest(
        format_version=1,
        subject="biologia",
        topic="la-celula",
        language="es",
        stt_mode="server",
        started_client_time_ms=1_750_000_000_000,
    )
    pcm = tone(2)
    with RecordingWriter(source_dir, manifest) as writer:
        writer.append_audio(pcm)
    recordings = tmp_path / "recordings"
    live = make_app(server, codes, tmp_path, tmp_vault, SERVER_STT, SessionRecorder(recordings))
    providers: list[FakeProvider] = []

    def provider_factory(settings: SttSettings) -> SpeechToTextProvider:
        provider = FakeProvider(language=settings.language, segments=HEARD)
        providers.append(provider)
        return provider

    live.state.gateway.provider_factory = provider_factory

    run_replay(live, read_recording(source_dir))

    [directory] = list(recordings.iterdir())
    recorded = read_recording(directory)
    assert recorded.manifest.stt_mode == "server"
    assert recorded.transcript == ()
    audio = recorded.read_audio()
    # The audio fed to the provider, after the silence between the session start and `hello`.
    assert audio.endswith(pcm)
    lead = audio[: len(audio) - len(pcm)]
    assert lead == bytes(len(lead)) and len(lead) < 16000 * 2


def test_resent_transcript_messages_are_recorded_once(ws: WsHarness, tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path / "recordings")
    ws.app.state.gateway.recorder = recorder
    segment: dict[str, Any] = {
        "segment_id": "s1",
        "client_start_ms": 1_000_500,
        "client_end_ms": 1_001_500,
        "provider": "web-speech",
        "language": "es-ES",
    }
    with ws.connect() as websocket:
        websocket.send_json(ws.hello())
        assert websocket.receive_json()["type"] == "hello.ack"
        websocket.send_json({**segment, "type": "transcript.client.partial", "text": "hola"})
        websocket.send_json({**segment, "type": "transcript.client.final", "text": "Hola."})
        websocket.send_json({**segment, "type": "transcript.client.final", "text": "Hola."})
        websocket.send_json({**segment, "type": "transcript.client.partial", "text": "hola"})
        websocket.send_json({"type": "marker", "label": "importante", "client_time_ms": 1_002_000})
        # Messages are handled in order: once this later final is echoed, all the above are.
        websocket.send_json(
            {**segment, "segment_id": "s2", "type": "transcript.client.final", "text": "Fin."}
        )
        while websocket.receive_json().get("segment_id") != "s2":
            pass

    directory = recorder.directory(ws.session_id)
    assert directory is not None
    recorded = read_recording(directory)
    first = [m for m in recorded.transcript if m.segment_id == "s1"]
    assert [type(m) for m in first] == [TranscriptClientPartial, TranscriptClientFinal]
    assert [m.type for m in recorded.events] == ["marker"]
    assert recorded.manifest.started_client_time_ms == 1_000_000 + ws.started_at_ms - ws.clock.now


def test_without_a_recorder_nothing_is_recorded(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    app = make_app(server, codes, tmp_path, tmp_vault, CLIENT_STT)

    run_replay(app, read_recording(FIXTURE))

    assert app.state.recorder is None
    assert app.state.gateway.recorder is None


def test_a_recordings_directory_inside_the_vault_is_refused(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    with pytest.raises(ValueError, match="inside the vault"):
        make_app(
            server,
            codes,
            tmp_path,
            tmp_vault,
            CLIENT_STT,
            SessionRecorder(tmp_vault.path / "recordings"),
        )


def test_resuming_after_a_restart_records_into_a_new_directory(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    (recordings / "sess-1").mkdir(parents=True)

    assert SessionRecorder(recordings)._free_directory("sess-1") == recordings / "sess-1-2"


@pytest.fixture
def uvicorn_apps(monkeypatch: pytest.MonkeyPatch) -> list[FastAPI]:
    """Stand in for `uvicorn.run`: `serve` hands over its app and returns at once."""
    import uvicorn

    apps: list[FastAPI] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kwargs: apps.append(app))
    return apps


def test_serve_record_records_under_the_configured_recordings_dir(
    uvicorn_apps: list[FastAPI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_SERVER__RECORDINGS_DIR", str(tmp_path / "recs"))
    monkeypatch.setenv("SA_SERVER__DEVICES_PATH", str(tmp_path / "state" / "devices.json"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "vault"))

    recorded = CliRunner().invoke(cli, ["serve", "--record"])
    plain = CliRunner().invoke(cli, ["serve"])

    assert recorded.exit_code == 0, recorded.output
    assert plain.exit_code == 0, plain.output
    with_record, without = uvicorn_apps
    assert with_record.state.recorder.root == tmp_path / "recs"
    assert with_record.state.gateway.recorder is with_record.state.recorder
    assert without.state.recorder is None
    assert not (tmp_path / "recs").exists()  # nothing is recorded before a session runs


def test_serve_record_refuses_a_recordings_dir_inside_the_vault(
    uvicorn_apps: list[FastAPI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_SERVER__RECORDINGS_DIR", str(tmp_path / "vault" / "recs"))
    monkeypatch.setenv("SA_SERVER__DEVICES_PATH", str(tmp_path / "state" / "devices.json"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "vault"))

    result = CliRunner().invoke(cli, ["serve", "--record"])

    assert result.exit_code == 1
    assert "inside the vault" in result.output
    assert uvicorn_apps == []
