"""The page transcriber inside `create_app`: the sample recording replayed with `FakeClaude`.

The app builds the transcriber only with an `llm_transport`. The sample's one capture waits for
its transcript window, which the session end cuts short: the end flushes the transcriber before
`session.ended`, so the transcription and its pending items land in the session's `events.jsonl`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI

from studentassistant.config import (
    ObserverSettings,
    ServerSettings,
    Settings,
    SourcesSettings,
    SttSettings,
)
from studentassistant.llm import FakeClaude
from studentassistant.observer import CAPTURE_EVENT_KIND, STATE_OP_EVENT_KIND
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import AsgiTransport, ReplayResult, replay
from studentassistant.sources.catchup import transcription_path
from studentassistant.sources.transcriber import PAGE_TRANSCRIBED_KIND
from studentassistant.vault import (
    Event,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    read_jsonl,
    read_ledger,
    start_session,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
STT = SttSettings(mode="client", provider="web-speech", language="es")
NO_OBSERVER = Settings(observer=ObserverSettings(enabled=False))


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def _app(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, **options: object
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        stt=STT,
        **options,  # type: ignore[arg-type]
    )


def _replay(app: FastAPI) -> ReplayResult:
    async def main() -> ReplayResult:
        async with AsgiTransport(app) as transport:
            return await asyncio.wait_for(
                replay(read_recording(FIXTURE), transport, speed=10, sleep=_no_wait), 30
            )

    return asyncio.run(main())


def test_without_an_llm_transport_or_when_disabled_there_is_no_transcriber(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    assert _app(server, codes, tmp_path, tmp_vault).state.transcriber is None
    disabled = _app(
        server,
        codes,
        tmp_path,
        tmp_vault,
        llm_transport=FakeClaude(),
        llm_settings=NO_OBSERVER,
        sources=SourcesSettings(transcription_enabled=False),
    )
    assert disabled.state.transcriber is None


def test_the_replayed_capture_is_transcribed_before_the_session_ends(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    fake = FakeClaude().reply_text("# La célula\n\n- membrana, [[?citoplasma]] y núcleo")
    app = _app(server, codes, tmp_path, tmp_vault, llm_transport=fake, llm_settings=NO_OBSERVER)

    result = _replay(app)

    [request] = fake.requests
    assert request.role == "transcriber"
    assert request.messages[0]["content"][0]["type"] == "image"
    hints = request.messages[0]["content"][-1]["text"]
    assert "membrana, citoplasma y núcleo" in hints  # said 2.5-7 s before the photo

    topic = tmp_vault.path / "subjects" / "biologia" / "topics" / "la-celula"
    events = list(read_jsonl(topic / "sessions" / result.session_id / "events.jsonl", Event))
    [done] = [e for e in events if e.kind == PAGE_TRANSCRIBED_KIND]
    [pending] = [e for e in events if e.kind == STATE_OP_EVENT_KIND]
    ended = next(e.seq for e in events if e.kind == "session.ended")
    assert pending.seq < done.seq < ended
    assert pending.payload["pending_id"] in done.payload["pending_ids"]
    assert (tmp_vault.path / done.payload["path"]).is_file()
    assert {entry.role for entry in read_ledger(tmp_vault, "biologia", "la-celula")} == {
        "transcriber"
    }


def test_at_server_start_an_untranscribed_page_of_the_last_session_is_transcribed(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    # A session the previous run ended while its only page was still waiting for Claude.
    subject = create_subject(tmp_vault, "Biología").slug
    topic = create_topic(tmp_vault, subject, "La célula").slug
    session = start_session(tmp_vault, subject, topic, "pc", PROTOCOL_VERSION)
    stored = put_source(tmp_vault, subject, topic, "notes", "foto.jpg", b"\xff\xd8 not decoded", {})
    source_path = stored.relative_to(tmp_vault.path).as_posix()
    session.append_event(
        CAPTURE_EVENT_KIND, "phone", {"capture_id": "cap-1", "source_path": source_path}
    )
    end_session(session)

    fake = FakeClaude().reply_text("# La célula")
    app = _app(server, codes, tmp_path, tmp_vault, llm_transport=fake, llm_settings=NO_OBSERVER)

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transcriber = app.state.transcriber
            await app.state.sessions.open_vault()  # the first request that needs the vault
            await asyncio.wait_for(transcriber.wait_startup(), 10)
            await asyncio.wait_for(transcriber.wait_idle(session.id), 10)

    asyncio.run(main())

    [request] = fake.requests
    assert request.role == "transcriber"
    assert (tmp_vault.path / transcription_path(source_path)).is_file()
