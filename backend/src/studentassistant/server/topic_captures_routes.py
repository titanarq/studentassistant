"""`GET ./captures` of a topic: its earlier photos for the capture client's strip (#580).

Read-only and per user (the active user's vault, like every read route). It lists the topic's
stored captures -- the `notes`/`book` pages a capture burst made, recognised by the `capture_id`
in their sidecar -- oldest first, at most `MAX_CAPTURES` (the newest ones). A retired source and a
capture the triage set aside (blurry, repeated) are left out. Each item names the capture, its
session, its phone-side time and a thumbnail route:

`GET .../topics/{topic}/captures/{capture_id}/thumbnail`: that capture's still, downscaled to at
most `THUMBNAIL_PX` on its long side and re-encoded as JPEG, so the client never downloads or
decodes the full-resolution photo.

These two routes are not versioned bodies of `protocol/`: they are additive read routes (like the
web's topic source list) that the Android strip consumes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field

from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.read_routes import (
    UNKNOWN_TOPIC_DETAIL,
    SubjectId,
    TopicId,
)
from studentassistant.server.user_scope import active_user_vault
from studentassistant.vault import (
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    list_sources,
    read_source,
)

MAX_CAPTURES = 40
"""The most captures one listing returns (the newest)."""
THUMBNAIL_PX = 256
"""The long side of a served thumbnail."""
UNKNOWN_CAPTURE_DETAIL = "No existe esa captura en este tema."
CAPTURE_KINDS = ("notes", "book")

CaptureId = Annotated[str, Path(pattern=ID_PATTERN)]


class TopicCapture(BaseModel):
    """One earlier photo of the topic."""

    capture_id: str
    session_id: str | None = Field(
        default=None, description="The session it was taken in, when its sidecar says."
    )
    captured_at_ms: int = Field(ge=0, description="Phone time of the capture, Unix epoch ms.")
    thumbnail_url: str = Field(description="The thumbnail route, relative to the backend.")


class TopicCaptures(BaseModel):
    """`GET /api/subjects/{subject_id}/topics/{topic_id}/captures`."""

    subject_id: str
    topic_id: str
    captures: list[TopicCapture] = Field(description="Oldest first, at most `MAX_CAPTURES`.")


def _captured_ms(value: Any) -> int:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return max(0, int(moment.timestamp() * 1000))
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return 0
        return _captured_ms(moment)
    return 0


def _listed(vault: Vault, subject_id: str, topic_id: str) -> list[tuple[str, str | None, int, str]]:
    """(capture_id, session, captured_at_ms, vault path) of every listed capture, oldest first."""
    rows: list[tuple[str, str | None, int, str]] = []
    for source in list_sources(vault, subject_id, topic_id):
        meta = source.meta or {}
        capture_id = meta.get("capture_id")
        if source.kind not in CAPTURE_KINDS or not isinstance(capture_id, str):
            continue
        triage = meta.get("triage")
        if isinstance(triage, dict) and triage.get("status") == "set_aside":
            continue
        session = meta.get("session")
        rows.append(
            (
                capture_id,
                session if isinstance(session, str) else None,
                _captured_ms(meta.get("captured_at")),
                source.path,
            )
        )
    rows.sort(key=lambda row: row[2])
    return rows


def _thumbnail(content: bytes) -> bytes | None:
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = THUMBNAIL_PX / max(height, width)
    if scale < 1:
        image = cv2.resize(
            image, (max(1, round(width * scale)), max(1, round(height * scale))), cv2.INTER_AREA
        )
    done, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return encoded.tobytes() if done else None


def _thumbnail_of(vault: Vault, subject_id: str, topic_id: str, capture_id: str) -> bytes | None:
    for found, _session, _at, path in _listed(vault, subject_id, topic_id):
        if found == capture_id:
            return _thumbnail(read_source(vault, path).content)
    return None


def topic_captures_router() -> APIRouter:
    """The two read routes; the vault is the active user's."""
    router = APIRouter(prefix="/api")

    @router.get(
        "/subjects/{subject_id}/topics/{topic_id}/captures",
        responses={404: {"description": "Unknown topic."}},
    )
    async def topic_captures(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> TopicCaptures:
        vault, _sync = await active_user_vault(request)
        try:
            rows = await asyncio.to_thread(_listed, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        base = f"/api/subjects/{subject_id}/topics/{topic_id}/captures"
        return TopicCaptures(
            subject_id=subject_id,
            topic_id=topic_id,
            captures=[
                TopicCapture(
                    capture_id=capture_id,
                    session_id=session,
                    captured_at_ms=at_ms,
                    thumbnail_url=f"{base}/{capture_id}/thumbnail",
                )
                for capture_id, session, at_ms, _path in rows[-MAX_CAPTURES:]
            ],
        )

    @router.get(
        "/subjects/{subject_id}/topics/{topic_id}/captures/{capture_id}/thumbnail",
        response_class=Response,
        responses={
            200: {"description": "The thumbnail JPEG.", "content": {"image/jpeg": {}}},
            404: {"description": "Unknown topic or capture."},
        },
    )
    async def capture_thumbnail(
        request: Request, subject_id: SubjectId, topic_id: TopicId, capture_id: CaptureId
    ) -> Response:
        vault, _sync = await active_user_vault(request)
        try:
            jpeg = await asyncio.to_thread(_thumbnail_of, vault, subject_id, topic_id, capture_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        if jpeg is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_CAPTURE_DETAIL)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

    return router
