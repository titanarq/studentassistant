"""The web's live session view: `GET /api/live`, a read-only Server-Sent Events stream.

While a session runs, the PC browser follows its transcript, its captured pages and the outline
the observer is building (#57). The stream follows the **active** session:

- first a `snapshot` of it (`session` is `null` when none is active, and the stream then ends:
  the browser's `EventSource` reconnects after `retry` milliseconds and asks again);
- then one event per change, as the session bus delivers it: `segment` (a final transcript
  segment), `partial` (a transcript partial, never stored), `capture` (a captured page, added or
  updated when its transcription lands or fails), `outline` (the whole outline and the open
  pending count, after an observer state op);
- `ended` when the session ends, and the stream ends with it.

Every event's `data` is one line of JSON (`LiveSnapshot`, `LiveSegment`, `LiveCapture`,
`LiveOutline`, `LiveEnded`). A comment line (`: keep-alive`) goes out every `keepalive` seconds
so a vanished browser is noticed. Nothing here writes: the log is read through the session's vault
handle and the outline is the observer's fold (`load_observer_snapshot(write_back=False)`), both
in worker threads. The route sits behind the LAN guard, the Host allowlist and the bearer check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from studentassistant.observer import (
    STATE_OP_EVENT_KIND,
    TopicState,
    load_observer_snapshot,
)
from studentassistant.server.bus import SessionBus, Subscription
from studentassistant.server.revise_routes import sse
from studentassistant.server.sessions import (
    SESSION_ENDED,
    OpenSession,
    SessionService,
)
from studentassistant.vault import Session

logger = logging.getLogger(__name__)

TRANSCRIPT_FINAL = "transcript.final"
TRANSCRIPT_PARTIAL = "transcript.partial"
CAPTURE_STORED = "capture.stored"
PAGE_TRANSCRIBED = "page.transcribed"
PAGE_TRANSCRIPTION_FAILED = "page.transcription_failed"
LIVE_KINDS = frozenset(
    {
        TRANSCRIPT_FINAL,
        TRANSCRIPT_PARTIAL,
        CAPTURE_STORED,
        PAGE_TRANSCRIBED,
        PAGE_TRANSCRIPTION_FAILED,
        STATE_OP_EVENT_KIND,
        SESSION_ENDED,
    }
)
"""The bus event kinds the live view reads."""

RETRY_MS = 3000
"""What the stream tells `EventSource` to wait before reconnecting."""
KEEPALIVE_SECONDS = 15.0


class LiveSession(BaseModel):
    session_id: str
    subject_id: str
    topic_id: str
    started_at_ms: int


class LiveSegment(BaseModel):
    """A transcript segment (final, or the current partial) in session milliseconds."""

    segment_id: str
    t_start: int
    t_end: int
    text: str


class LiveCapture(BaseModel):
    """A captured page: `pending` until its transcription lands (`transcribed`) or fails."""

    capture_id: str
    t: int | None = None
    page_path: str | None = None
    source_path: str | None = None
    source_context: str | None = None
    status: Literal["pending", "transcribed", "failed"] = "pending"
    text: str | None = None
    page_number: int | None = None
    message: str | None = None


class LiveSection(BaseModel):
    """One outline section; `segment_count` is how many segments the observer put in it."""

    section_id: str
    title: str
    parent_id: str | None = None
    segment_count: int = 0


class LiveOutline(BaseModel):
    outline: list[LiveSection]
    open_pending: int


class LiveSnapshot(BaseModel):
    session: LiveSession | None
    segments: list[LiveSegment]
    captures: list[LiveCapture]
    outline: list[LiveSection]
    open_pending: int


class LiveEnded(BaseModel):
    session_id: str


def segment_of(payload: Mapping[str, Any]) -> LiveSegment | None:
    """A transcript event's segment, None when its payload is not usable."""
    segment_id = payload.get("segment_id")
    text = payload.get("text")
    start = payload.get("session_start_ms")
    end = payload.get("session_end_ms", start)
    if not isinstance(segment_id, str) or not isinstance(text, str):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return LiveSegment(segment_id=segment_id, t_start=start, t_end=max(start, end), text=text)


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def apply_capture_event(
    captures: dict[str, LiveCapture], kind: str, t: int, payload: Mapping[str, Any]
) -> LiveCapture | None:
    """Fold a capture/page event into `captures` (by id); the changed capture, None if unusable."""
    capture_id = payload.get("capture_id")
    if not isinstance(capture_id, str):
        return None
    current = captures.get(capture_id) or LiveCapture(capture_id=capture_id)
    if kind == CAPTURE_STORED:
        if capture_id in captures:
            return None  # a repeated id keeps its first registration, as in the observer's fold
        current = current.model_copy(
            update={
                "t": t,
                "page_path": _text(payload, "page_path"),
                "source_path": _text(payload, "source_path"),
                "source_context": _text(payload, "source_context"),
            }
        )
    elif kind == PAGE_TRANSCRIBED:
        number = payload.get("page_number")
        update: dict[str, Any] = {
            "status": "transcribed",
            "text": _text(payload, "text"),
            "page_number": number if isinstance(number, int) else None,
            "message": None,
        }
        for key in ("page_path", "source_path", "source_context"):
            if getattr(current, key) is None:
                update[key] = _text(payload, key)
        current = current.model_copy(update=update)
    elif kind == PAGE_TRANSCRIPTION_FAILED:
        if current.status == "transcribed":
            return None
        current = current.model_copy(
            update={"status": "failed", "message": _text(payload, "message")}
        )
    else:
        return None
    captures[capture_id] = current
    return current


def outline_of(state: TopicState) -> LiveOutline:
    """The outline in reading order (each section followed by its subsections)."""
    sections: list[LiveSection] = []

    def walk(parent_id: str | None, seen: set[str]) -> None:
        for section in state.outline(parent_id):
            if section.id in seen:
                continue
            seen.add(section.id)
            sections.append(
                LiveSection(
                    section_id=section.id,
                    title=section.title,
                    parent_id=section.parent_id,
                    segment_count=len(state.segments_of(section.id)),
                )
            )
            walk(section.id, seen)

    walk(None, set())
    return LiveOutline(outline=sections, open_pending=len(state.open_pending()))


def _read_log(session: Session) -> tuple[int, list[LiveSegment], dict[str, LiveCapture]]:
    """The last seq, the final segments and the captures already in the session's log."""
    last_seq = 0
    segments: dict[str, LiveSegment] = {}
    captures: dict[str, LiveCapture] = {}
    for event in session.read_events():
        last_seq = max(last_seq, event.seq)
        if event.kind == TRANSCRIPT_FINAL:
            segment = segment_of(event.payload)
            if segment is not None and segment.segment_id not in segments:
                segments[segment.segment_id] = segment
        else:
            apply_capture_event(captures, event.kind, event.t, event.payload)
    return last_seq, list(segments.values()), captures


def _read_outline(session: Session) -> LiveOutline:
    try:
        snapshot = load_observer_snapshot(
            session.vault, session.subject_slug, session.topic_slug, write_back=False
        )
    except Exception:
        logger.warning(
            "the live view could not fold the observer state of %s/%s",
            session.subject_slug,
            session.topic_slug,
            exc_info=True,
        )
        return LiveOutline(outline=[], open_pending=0)
    return outline_of(snapshot.state)


def _event(name: str, model: BaseModel) -> bytes:
    return sse(name, model.model_dump(mode="json"))


def _empty_snapshot() -> LiveSnapshot:
    return LiveSnapshot(session=None, segments=[], captures=[], outline=[], open_pending=0)


async def live_events(
    service: SessionService,
    bus: SessionBus,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """The live view's stream (see the module doc) of the session active when it is called."""
    yield f"retry: {RETRY_MS}\n\n".encode()
    active: OpenSession | None = service.active
    session = None if active is None else bus.attached(active.session_id)
    if active is None or session is None:
        yield _event("snapshot", _empty_snapshot())
        return
    # Subscribe before reading the log, so nothing published meanwhile is missed; what the log
    # already holds is skipped by `seq`.
    subscription = bus.subscribe(
        name=f"live:{active.session_id}", session_id=active.session_id, kinds=LIVE_KINDS
    )
    try:
        last_seq, segments, captures = await asyncio.to_thread(_read_log, session)
        outline = await asyncio.to_thread(_read_outline, session)
        yield _event(
            "snapshot",
            LiveSnapshot(
                session=LiveSession(
                    session_id=active.session_id,
                    subject_id=active.subject_id,
                    topic_id=active.topic_id,
                    started_at_ms=active.started_at_ms,
                ),
                segments=segments,
                captures=list(captures.values()),
                outline=outline.outline,
                open_pending=outline.open_pending,
            ),
        )
        async for chunk in _follow(subscription, session, captures, last_seq, keepalive):
            yield chunk
    finally:
        subscription.close()


async def _follow(
    subscription: Subscription,
    session: Session,
    captures: dict[str, LiveCapture],
    last_seq: int,
    keepalive: float,
) -> AsyncIterator[bytes]:
    finals: set[str] = set()
    while True:
        try:
            event = await asyncio.wait_for(subscription.get(), timeout=keepalive)
        except TimeoutError:
            yield b": keep-alive\n\n"
            continue
        except Exception:  # the subscription was closed (shutdown)
            return
        if event.seq is not None and event.seq <= last_seq:
            continue
        if event.kind == SESSION_ENDED:
            yield _event("ended", LiveEnded(session_id=event.session_id))
            return
        for chunk in await _chunks(event.kind, event.t, event.payload, session, captures, finals):
            yield chunk


async def _chunks(
    kind: str,
    t: int,
    payload: Mapping[str, Any],
    session: Session,
    captures: dict[str, LiveCapture],
    finals: set[str],
) -> Iterable[bytes]:
    if kind in (TRANSCRIPT_FINAL, TRANSCRIPT_PARTIAL):
        segment = segment_of(payload)
        if segment is None:
            return []
        if kind == TRANSCRIPT_PARTIAL:
            return [] if segment.segment_id in finals else [_event("partial", segment)]
        if segment.segment_id in finals:
            return []
        finals.add(segment.segment_id)
        return [_event("segment", segment)]
    if kind == STATE_OP_EVENT_KIND:
        return [_event("outline", await asyncio.to_thread(_read_outline, session))]
    changed = apply_capture_event(captures, kind, t, payload)
    return [] if changed is None else [_event("capture", changed)]


def live_router() -> APIRouter:
    """`GET /api/live`; the service and bus are read from `app.state.sessions` / `.bus`."""
    router = APIRouter(prefix="/api")

    @router.get("/live", response_class=StreamingResponse)
    async def live(request: Request) -> StreamingResponse:
        return StreamingResponse(
            live_events(request.app.state.sessions, request.app.state.bus),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
