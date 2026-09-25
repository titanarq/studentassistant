"""The web's live session view: `GET /api/live` (SSE) and the stream it serves."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.server.app import create_app
from studentassistant.server.bus import SessionBus
from studentassistant.server.live_routes import live_events
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import SessionService
from studentassistant.vault import Vault

pytestmark = pytest.mark.anyio

WAIT = 5.0  # seconds; every read of the stream is bounded, so a broken stream fails, never hangs


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _parse(chunk: bytes) -> tuple[str, Any]:
    """One SSE chunk as `(event, data)`; `("retry", ms)` and `("comment", text)` for the others."""
    text = chunk.decode()
    assert text.endswith("\n\n")
    fields: dict[str, str] = {}
    for line in text.strip("\n").splitlines():
        if line.startswith(":"):
            return "comment", line[1:].strip()
        name, value = line.split(": ", 1)
        fields[name] = value
    if "retry" in fields:
        return "retry", int(fields["retry"])
    return fields["event"], json.loads(fields["data"])


async def _next(stream: AsyncIterator[bytes]) -> tuple[str, Any]:
    return _parse(await asyncio.wait_for(anext(stream), WAIT))


async def _ends(stream: AsyncIterator[bytes]) -> None:
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), WAIT)


def _segment(segment_id: str, start: int, text: str) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "session_start_ms": start,
        "session_end_ms": start + 900,
        "text": text,
        "language": "es",
        "provider": "fake",
    }


PAGE_PATH = "subjects/fisica/topics/cinematica/sources/notes/page-001.page.jpg"


async def _started(service: SessionService) -> str:
    subject = await service.create_subject("Física")
    topic = await service.create_topic(subject.subject_id, "Cinemática")
    session = await service.start(subject.subject_id, topic.topic_id, client_time_ms=0)
    return session.session_id


async def test_without_an_active_session_the_stream_is_an_empty_snapshot(
    tmp_vault: Vault,
) -> None:
    bus = SessionBus()
    stream = live_events(SessionService(bus, vault=tmp_vault, host="pc"), bus)
    assert await _next(stream) == ("retry", 3000)
    assert await _next(stream) == (
        "snapshot",
        {"session": None, "segments": [], "captures": [], "outline": [], "open_pending": 0},
    )
    await _ends(stream)


async def test_the_stream_follows_the_active_session_until_it_ends(tmp_vault: Vault) -> None:
    bus = SessionBus()
    service = SessionService(bus, vault=tmp_vault, host="pc")
    session_id = await _started(service)
    # Already in the log before the browser connects: part of the snapshot.
    await bus.publish(session_id, "transcript.final", "stt", _segment("seg-1", 0, "Hoy vemos MRU"))
    await bus.publish(
        session_id,
        "capture.stored",
        "phone",
        {
            "capture_id": "cap-1",
            "page_path": PAGE_PATH,
            "source_path": PAGE_PATH.replace(".page.jpg", ".jpg"),
            "source_context": "notes",
        },
        t=1200,
    )
    await bus.publish(
        session_id,
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "add_section", "section_id": "mru", "title": "Movimiento rectilíneo"},
    )

    stream = live_events(service, bus, keepalive=WAIT)
    assert await _next(stream) == ("retry", 3000)
    name, snapshot = await _next(stream)
    assert name == "snapshot"
    assert snapshot["session"]["session_id"] == session_id
    assert (snapshot["session"]["subject_id"], snapshot["session"]["topic_id"]) == (
        "fisica",
        "cinematica",
    )
    assert snapshot["segments"] == [
        {"segment_id": "seg-1", "t_start": 0, "t_end": 900, "text": "Hoy vemos MRU"}
    ]
    [capture] = snapshot["captures"]
    assert capture["capture_id"] == "cap-1"
    assert (capture["status"], capture["page_path"], capture["t"]) == ("pending", PAGE_PATH, 1200)
    assert snapshot["outline"] == [
        {
            "section_id": "mru",
            "title": "Movimiento rectilíneo",
            "parent_id": None,
            "segment_count": 0,
        }
    ]
    assert snapshot["open_pending"] == 0

    partial = _segment("seg-2", 1000, "la velo")
    await bus.publish(session_id, "transcript.partial", "stt", partial, persist=False)
    assert await _next(stream) == (
        "partial",
        {"segment_id": "seg-2", "t_start": 1000, "t_end": 1900, "text": "la velo"},
    )
    await bus.publish(
        session_id, "transcript.final", "stt", _segment("seg-2", 1000, "la velocidad es constante")
    )
    assert (await _next(stream))[1]["text"] == "la velocidad es constante"
    # A late partial of a segment already final is not shown again.
    await bus.publish(session_id, "transcript.partial", "stt", partial, persist=False)

    await bus.publish(
        session_id,
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "add_section", "section_id": "graficas", "title": "Gráficas", "parent_id": "mru"},
    )
    name, outline = await _next(stream)
    assert name == "outline"
    assert [s["section_id"] for s in outline["outline"]] == ["mru", "graficas"]
    await bus.publish(
        session_id,
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "assign_segments", "section_id": "graficas", "segment_ids": ["seg-1", "seg-2"]},
    )
    await bus.publish(
        session_id,
        STATE_OP_EVENT_KIND,
        "observer",
        {
            "op": "add_pending",
            "pending_id": "p-1",
            "kind": "illegible",
            "text": "No se lee la fórmula de la página 1",
            "segment_ids": [],
            "capture_ids": ["cap-1"],
            "source_refs": [],
        },
    )
    assert (await _next(stream))[1]["outline"][1]["segment_count"] == 2
    assert (await _next(stream))[1]["open_pending"] == 1

    await bus.publish(
        session_id,
        "page.transcribed",
        "stt",
        {"capture_id": "cap-1", "text": "MRU: v = cte", "page_number": 1, "page_path": PAGE_PATH},
    )
    name, capture = await _next(stream)
    assert name == "capture"
    assert (capture["status"], capture["text"], capture["page_number"]) == (
        "transcribed",
        "MRU: v = cte",
        1,
    )
    assert capture["t"] == 1200  # kept from `capture.stored`
    await bus.publish(
        session_id,
        "page.transcription_failed",
        "stt",
        {"capture_id": "cap-2", "reason": "error", "message": "sin respuesta"},
    )
    name, failed = await _next(stream)
    assert (name, failed["capture_id"], failed["status"]) == ("capture", "cap-2", "failed")
    assert failed["message"] == "sin respuesta"

    await service.end(session_id, client_time_ms=5000, reason="button")
    assert await _next(stream) == ("ended", {"session_id": session_id})
    await _ends(stream)


async def test_a_quiet_stream_sends_keep_alive_comments(tmp_vault: Vault) -> None:
    bus = SessionBus()
    service = SessionService(bus, vault=tmp_vault, host="pc")
    await _started(service)
    stream = aiter(live_events(service, bus, keepalive=0.01))
    assert isinstance(stream, AsyncGenerator)
    await _next(stream)
    assert (await _next(stream))[0] == "snapshot"
    assert await _next(stream) == ("comment", "keep-alive")
    await stream.aclose()


async def test_a_closed_bus_ends_the_stream(tmp_vault: Vault) -> None:
    bus = SessionBus()
    service = SessionService(bus, vault=tmp_vault, host="pc")
    await _started(service)
    stream = live_events(service, bus, keepalive=WAIT)
    await _next(stream)
    await _next(stream)
    bus.close()
    await _ends(stream)


def test_the_route_serves_the_stream(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/live")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    chunks = [chunk + "\n\n" for chunk in response.text.split("\n\n") if chunk]
    assert [_parse(chunk.encode())[0] for chunk in chunks] == ["retry", "snapshot"]
