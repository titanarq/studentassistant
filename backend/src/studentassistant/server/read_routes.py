"""The web UI's read API under `/api`: study-desk summary, notes, pending review, sources, sessions,
transcripts.

Read-only and thin: every route opens the vault through the `SessionService` on
`app.state.sessions` (so the lazily opened, pulled vault is the one the lifecycle routes use) and
reads it only through the vault's public functions (ADR-0002), each call in a worker thread.
Nothing here opens a file of the vault itself: a source is served from what `read_source` returns,
which refuses any path outside a topic's `sources/<kind>/`.

Errors, as `{"detail": "..."}` with a Spanish `detail`: an unknown subject, topic, session or
source (or a source path that is not one) is 404, a path id outside the protocol's id pattern or a
malformed transcript span 422, and a vault that cannot be opened 503. Every route sits behind the
LAN guard, the Host allowlist and the bearer check (none is exempt).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field

from studentassistant.observer import PendingItem, load_observer_snapshot, pending_review
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    SOURCE_KINDS,
    GitSync,
    SessionMeta,
    SessionNotFoundError,
    SourceNotFoundError,
    SourcePathError,
    SubjectNotFoundError,
    TopicNotFoundError,
    TranscriptSegment,
    Vault,
    list_generated,
    list_sessions,
    list_sources,
    read_notes,
    read_session_transcript,
    read_source,
)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]
SessionId = Annotated[str, Path(pattern=ID_PATTERN)]

UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
UNKNOWN_SESSION_DETAIL = "No existe esa sesión en ese tema."
UNKNOWN_SOURCE_DETAIL = "No existe esa fuente en la bóveda."
NO_NOTES_DETAIL = "Todavía no hay apuntes de este tema."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
BAD_SPAN_DETAIL = "El tramo debe tener la forma HH:MM:SS-HH:MM:SS, con el inicio antes del final."

SPAN_PATTERN = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d)-(\d{2,}):([0-5]\d):([0-5]\d)$")
"""A transcript span as ADR-0005's provenance refs write it: `sessions/<id>#t=00:02:34-00:03:10`."""

# Media types a source is served with as they are; any other `text/*` goes out as `text/plain` and
# anything else (HTML, SVG, scripts, unknown) as `application/octet-stream`, so no source is ever
# rendered by the browser as a document of the backend's origin.
_SERVED_AS_IS = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
        "application/pdf",
        "text/markdown",
        "text/plain",
    }
)
_SOURCE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
}


# -- response bodies -----------------------------------------------------------------------------


class SourceCounts(BaseModel):
    """How many sources a topic has of each kind."""

    notes: int = 0
    book: int = 0
    pdf: int = 0
    web: int = 0


class TopicSummary(BaseModel):
    """`GET /api/subjects/{subject_id}/topics/{topic_id}/summary`: the study desk's card."""

    subject_id: str
    topic_id: str
    sources: SourceCounts
    sessions: int = Field(description="Sessions of the topic, open or ended.")
    session_minutes: float = Field(
        description="Their total length in minutes; an unended session counts up to now."
    )
    open_pending: int = Field(description="Open pending-review items of the observer's state.")
    notes_version: int | None = Field(
        description="The highest `<subject>/<topic>/apuntes-vN` tag, `null` when there is none."
    )
    generated: list[str] = Field(
        description="Vault-relative paths of the generated material under `generated/`."
    )


class TopicNotes(BaseModel):
    """`GET /api/subjects/{subject_id}/topics/{topic_id}/notes`: the master notes."""

    subject_id: str
    topic_id: str
    text: str = Field(description="`notes/apuntes.md` as Markdown.")
    version: int | None = Field(description="The notes version, `null` when none is tagged.")


class TopicPending(BaseModel):
    """`GET /api/subjects/{subject_id}/topics/{topic_id}/pending`: the pending-review queue."""

    subject_id: str
    topic_id: str
    open_count: int = Field(description="Open items of the whole queue, whatever the filter.")
    items: list[PendingItem] = Field(
        description="The items the filter keeps: open ones first, each group in the order added."
    )


PendingFilter = Literal["all", "open", "closed"]


class SourceMeta(BaseModel):
    """`GET /api/sources/{vault_id}/meta`: a source's sidecar metadata."""

    vault_id: str = Field(description="The source's vault-relative path.")
    kind: str
    media_type: str = Field(description="The media type `GET /api/sources/{vault_id}` serves.")
    size: int = Field(description="Bytes of the content.")
    meta: dict[str, Any] | None = Field(description="The parsed sidecar, `null` without one.")
    transcription: str | None = Field(
        description="The sidecar's `transcription` when it carries one as text."
    )


class SessionSummary(BaseModel):
    """One session of a topic."""

    session_id: str
    started_at: datetime
    ended_at: datetime | None
    minutes: float = Field(description="Its length in minutes; an unended one counts up to now.")


class TopicSessions(BaseModel):
    """`GET /api/subjects/{subject_id}/topics/{topic_id}/sessions`, sessions by id."""

    subject_id: str
    topic_id: str
    sessions: list[SessionSummary]


class TranscriptLine(BaseModel):
    """One final transcript segment, its times in milliseconds since the session started."""

    seq: int
    t_start: int
    t_end: int
    text: str


class TranscriptSpan(BaseModel):
    """`GET /api/sessions/{session_id}/transcript`: the segments overlapping a span."""

    session_id: str
    subject_id: str
    topic_id: str
    ref: str = Field(description="The provenance ref of the span, `sessions/<id>#t=<span>`.")
    start_ms: int
    end_ms: int
    segments: list[TranscriptLine]


# -- helpers -------------------------------------------------------------------------------------


def parse_span(span: str) -> tuple[int, int]:
    """`HH:MM:SS-HH:MM:SS` as `(start_ms, end_ms)`; `ValueError` when malformed or reversed."""
    match = SPAN_PATTERN.fullmatch(span)
    if match is None:
        raise ValueError(span)
    h1, m1, s1, h2, m2, s2 = (int(group) for group in match.groups())
    start = ((h1 * 60 + m1) * 60 + s1) * 1000
    end = ((h2 * 60 + m2) * 60 + s2) * 1000
    if start > end:
        raise ValueError(span)
    return start, end


def overlapping(
    segments: list[TranscriptSegment], start_ms: int, end_ms: int
) -> list[TranscriptSegment]:
    """The segments sharing some time with `[start_ms, end_ms]`.

    A point span (`start_ms == end_ms`) takes the segments that span that instant.
    """
    if start_ms == end_ms:
        return [s for s in segments if s.t_start <= start_ms <= s.t_end]
    return [s for s in segments if s.t_start < end_ms and s.t_end > start_ms]


def _minutes(meta: SessionMeta, now: datetime) -> float:
    end = meta.ended_at if meta.ended_at is not None else now
    return round(max((end - meta.started_at).total_seconds(), 0.0) / 60, 1)


def _notes_version(sync: GitSync | None, subject_id: str, topic_id: str) -> int | None:
    tags = [] if sync is None else sync.list_notes_tags(subject_id, topic_id)
    return max((tag.version for tag in tags), default=None)


def _service(request: Request) -> SessionService:
    return request.app.state.sessions


async def _vault(request: Request) -> Vault:
    try:
        return await _service(request).open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
        ) from error


async def _read[T](function: Callable[..., T], *args: object) -> T:
    return await asyncio.to_thread(function, *args)


@asynccontextmanager
async def _not_found() -> AsyncIterator[None]:
    try:
        yield
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
    except SessionNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_SESSION_DETAIL) from error
    except (SourcePathError, SourceNotFoundError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_SOURCE_DETAIL) from error


def _served_media_type(media_type: str) -> str:
    if media_type in _SERVED_AS_IS:
        return f"{media_type}; charset=utf-8" if media_type.startswith("text/") else media_type
    if media_type.startswith("text/"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def _kind_of(vault_id: str) -> str:
    # `read_source` accepted it, so it is `subjects/<s>/topics/<t>/sources/<kind>/<file>`.
    return vault_id.split("/")[5]


# -- the router ----------------------------------------------------------------------------------


def read_router() -> APIRouter:
    """The web's read routes; the vault comes from `app.state.sessions`."""
    router = APIRouter(prefix="/api")

    @router.get("/subjects/{subject_id}/topics/{topic_id}/summary")
    async def topic_summary(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> TopicSummary:
        vault = await _vault(request)
        sync = _service(request).sync

        def summarise() -> TopicSummary:
            sources = list_sources(vault, subject_id, topic_id)
            counts = {kind: 0 for kind in SOURCE_KINDS}
            for source in sources:
                counts[source.kind] = counts.get(source.kind, 0) + 1
            sessions = list_sessions(vault, subject_id, topic_id)
            now = datetime.now(UTC)
            snapshot = load_observer_snapshot(vault, subject_id, topic_id)
            return TopicSummary(
                subject_id=subject_id,
                topic_id=topic_id,
                sources=SourceCounts.model_validate(counts),
                sessions=len(sessions),
                session_minutes=round(sum(_minutes(meta, now) for meta in sessions), 1),
                open_pending=len(snapshot.state.open_pending()),
                notes_version=_notes_version(sync, subject_id, topic_id),
                generated=list_generated(vault, subject_id, topic_id),
            )

        async with _not_found():
            return await _read(summarise)

    @router.get(
        "/subjects/{subject_id}/topics/{topic_id}/notes",
        responses={404: {"description": "Unknown topic, or no notes written yet."}},
    )
    async def topic_notes(request: Request, subject_id: SubjectId, topic_id: TopicId) -> TopicNotes:
        vault = await _vault(request)
        sync = _service(request).sync
        async with _not_found():
            text = await _read(read_notes, vault, subject_id, topic_id)
        if text is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NO_NOTES_DETAIL)
        version = await _read(_notes_version, sync, subject_id, topic_id)
        return TopicNotes(subject_id=subject_id, topic_id=topic_id, text=text, version=version)

    @router.get(
        "/subjects/{subject_id}/topics/{topic_id}/pending",
        responses={404: {"description": "Unknown topic."}},
    )
    async def topic_pending(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        which: Annotated[
            PendingFilter,
            Query(alias="status", description="`open`, `closed` (settled, dismissed) or `all`."),
        ] = "all",
    ) -> TopicPending:
        vault = await _vault(request)
        async with _not_found():
            snapshot = await _read(
                lambda: load_observer_snapshot(vault, subject_id, topic_id, write_back=False)
            )
        review = pending_review(snapshot.state)
        items = [
            item for item in review.items if which == "all" or item.is_open == (which == "open")
        ]
        return TopicPending(
            subject_id=subject_id, topic_id=topic_id, open_count=review.open_count, items=items
        )

    @router.get("/subjects/{subject_id}/topics/{topic_id}/sessions")
    async def topic_sessions(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> TopicSessions:
        vault = await _vault(request)
        async with _not_found():
            metas = await _read(list_sessions, vault, subject_id, topic_id)
        now = datetime.now(UTC)
        return TopicSessions(
            subject_id=subject_id,
            topic_id=topic_id,
            sessions=[
                SessionSummary(
                    session_id=meta.id,
                    started_at=meta.started_at,
                    ended_at=meta.ended_at,
                    minutes=_minutes(meta, now),
                )
                for meta in metas
            ],
        )

    @router.get(
        "/sessions/{session_id}/transcript",
        responses={404: {"description": "Unknown topic or session."}},
    )
    async def session_transcript(
        request: Request,
        session_id: SessionId,
        subject: Annotated[str, Query(pattern=ID_PATTERN)],
        topic: Annotated[str, Query(pattern=ID_PATTERN)],
        t: Annotated[str, Query(description="The span, `HH:MM:SS-HH:MM:SS`.")],
    ) -> TranscriptSpan:
        try:
            start_ms, end_ms = parse_span(t)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_SPAN_DETAIL) from error
        vault = await _vault(request)
        async with _not_found():
            segments = await _read(read_session_transcript, vault, subject, topic, session_id)
        return TranscriptSpan(
            session_id=session_id,
            subject_id=subject,
            topic_id=topic,
            ref=f"sessions/{session_id}#t={t}",
            start_ms=start_ms,
            end_ms=end_ms,
            segments=[
                TranscriptLine(seq=s.seq, t_start=s.t_start, t_end=s.t_end, text=s.text)
                for s in overlapping(segments, start_ms, end_ms)
            ],
        )

    # `/meta` first: the `path` converter of the content route would otherwise swallow it.
    @router.get(
        "/sources/{vault_id:path}/meta",
        responses={404: {"description": "No source at that vault-relative path."}},
    )
    async def source_meta(request: Request, vault_id: str) -> SourceMeta:
        vault = await _vault(request)
        async with _not_found():
            source = await _read(read_source, vault, vault_id)
        transcription = (source.meta or {}).get("transcription")
        return SourceMeta(
            vault_id=vault_id,
            kind=_kind_of(vault_id),
            media_type=_served_media_type(source.media_type),
            size=len(source.content),
            meta=source.meta,
            transcription=transcription if isinstance(transcription, str) else None,
        )

    @router.get(
        "/sources/{vault_id:path}",
        response_class=Response,
        responses={
            200: {
                "description": "The source's bytes.",
                "content": {"application/octet-stream": {}},
            },
            404: {"description": "No source at that vault-relative path."},
        },
    )
    async def source_content(request: Request, vault_id: str) -> Response:
        vault = await _vault(request)
        async with _not_found():
            source = await _read(read_source, vault, vault_id)
        return Response(
            content=source.content,
            media_type=_served_media_type(source.media_type),
            headers=_SOURCE_HEADERS,
        )

    return router
