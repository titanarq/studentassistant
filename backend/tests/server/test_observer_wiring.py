"""The live observer inside `create_app`: the sample recording replayed with `FakeClaude` as Sonnet.

The app gets the observer only with an `llm_transport`; the replay drives it exactly as a capture
client would, on a virtual clock, and the session end flushes the observer before
`session.ended`, so its ops land in the session's `events.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
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
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.observer.live import TOOL_NAME
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import AsgiTransport, ReplayResult, replay
from studentassistant.vault import Event, Vault, read_conversation, read_jsonl, read_ledger

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"
STT = SttSettings(mode="client", provider="web-speech", language="es")


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def _app(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    **options: object,
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


def test_without_an_llm_transport_the_app_has_no_observer(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    assert _app(server, codes, tmp_path, tmp_vault).state.observer is None


def test_a_disabled_observer_is_not_built(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    app = _app(
        server,
        codes,
        tmp_path,
        tmp_vault,
        llm_transport=FakeClaude(),
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    assert app.state.observer is None


def test_the_replayed_sample_yields_observer_ops_in_the_vault(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    fake = FakeClaude()
    fake.reply_tool(
        TOOL_NAME,
        {"ops": [{"op": "add_section", "section_id": "sec-1", "title": "La célula"}]},
    )
    for _ in range(5):  # more than the sample can ask for
        fake.reply_tool(TOOL_NAME, {"ops": []})
    app = _app(
        server,
        codes,
        tmp_path,
        tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings()),
        # The page transcriber would share the fake's script; `test_transcriber_wiring.py` has it.
        sources=SourcesSettings(transcription_enabled=False),
    )

    result = _replay(app)

    assert fake.requests, "the observer never called Claude"
    sent = "\n".join(json.dumps(r.messages, ensure_ascii=False) for r in fake.requests)
    # Every final reached the observer, the last one through the flush at the session end.
    for text in ("unidad básica", "membrana, citoplasma", "material genético"):
        assert text in sent
    assert "button switch_source" in sent and "capture " in sent

    topic = tmp_vault.path / "subjects" / "biologia" / "topics" / "la-celula"
    events = list(read_jsonl(topic / "sessions" / result.session_id / "events.jsonl", Event))
    ops = [e for e in events if e.kind == STATE_OP_EVENT_KIND]
    assert [(e.origin, e.payload["op"]) for e in ops] == [("observer", "add_section")]
    ended = next(e.seq for e in events if e.kind == "session.ended")
    assert all(e.seq < ended for e in ops)

    records = read_conversation(tmp_vault, "biologia", "la-celula", f"observer-{result.session_id}")
    assert records[0].kind == "context"
    assert sum(record.kind == "assistant" for record in records) == len(fake.requests)
    ledger = read_ledger(tmp_vault, "biologia", "la-celula")
    assert len(ledger) == len(fake.requests)
    assert {entry.role for entry in ledger} == {"observer"}
