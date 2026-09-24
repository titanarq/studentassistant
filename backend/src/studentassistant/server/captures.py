"""`POST /api/sessions/{id}/captures`: a burst of stills of protocol v1, stored as a source.

The body is `multipart/form-data`: a `metadata` part holding the `rest.sessions.captures.request`
JSON and one part per image, named by that JSON's `images[].part`. The body is parsed as it
streams in, never buffered whole: an image part over `[server].max_capture_image_bytes`, or more
image parts than `[server].max_capture_images`, stops the read at once with 413, so what is held
in memory is bounded by those two limits. A missing or invalid metadata part, an image part it
names that is absent, a part it does not name, or a part whose `Content-Type` is not the one it
declares is 422. Every refusal (`{"detail": "..."}`, Spanish) stores and publishes nothing.

The session must be the active one: unknown is 404, ended or not resumed is 409, a vault that
cannot be opened is 503. Until capture processing exists (the `sources` module), only the burst's
first image is stored, as it came, through `vault.put_source` with its sidecar metadata, under the
session's current source context (`current_source_context`: the `source` of its latest
`switch_source` button event, `notes` when there is none); then a persisted `capture.stored` event
(origin `phone`) is published on the bus, which is also what later uploads read to recognise a
repeated `capture_id` (`sessions.stored_captures`), and which the WebSocket gateway forwards to
the connected client as its capture `ack`.
A new capture answers 201 `stored`, a repeated one 200 `duplicate` storing and publishing nothing;
the check-and-store is serialised per session, so two concurrent uploads of one id store it once.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from studentassistant import protocol
from studentassistant.config import ServerSettings
from studentassistant.observer import CAPTURE_EVENT_KIND, CAPTURE_ID_KEY
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.bus import SessionNotAttachedError
from studentassistant.server.sessions import (
    SessionConflictError,
    SessionService,
    UnknownSessionError,
    VaultUnavailableError,
    stored_captures,
)
from studentassistant.vault import SecretRefused, Session, SessionEndedError, put_source

SessionId = Annotated[str, PathParam(pattern=ID_PATTERN)]

METADATA_PART = "metadata"
MAX_METADATA_BYTES = 64 * 1024
"""The largest `metadata` part accepted: the JSON of a burst is a few hundred bytes."""

_MULTIPART_OVERHEAD_BYTES = 64 * 1024
"""Room for boundaries and part headers on top of the parts' own bytes, in the total cap."""

DEFAULT_SOURCE_KIND = "notes"
"""The source context of a session before its first `switch_source` button."""

BUTTON_EVENT_KIND = "button"
SWITCH_SOURCE = "switch_source"
# Protocol v1 `button.source` -> the vault source kind a capture is stored under.
_SOURCE_KINDS = {"notes": "notes", "book": "book", "pdf": "pdf"}

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class _RefusedError(Exception):
    """An upload refused with `status_code` and a Spanish `detail`."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _unprocessable(detail: str) -> _RefusedError:
    return _RefusedError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _too_large(detail: str) -> _RefusedError:
    return _RefusedError(status.HTTP_413_CONTENT_TOO_LARGE, detail)


@dataclass
class _Part:
    name: str | None = None
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    data: bytearray = field(default_factory=bytearray)
    limit: int = 0


@dataclass
class _BurstReader:
    """Streaming multipart callbacks that enforce the part-count and part-size limits."""

    max_image_bytes: int
    max_images: int
    parts: dict[str, _Part] = field(default_factory=dict)
    ended: bool = False
    _current: _Part | None = None
    _field: bytearray = field(default_factory=bytearray)
    _value: bytearray = field(default_factory=bytearray)
    _images: int = 0

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_end": self._end,
        }

    def _part_begin(self) -> None:
        self._current = _Part()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        assert self._current is not None
        name = self._field.decode("latin-1").strip().lower()
        self._current.headers[name] = self._value.decode("latin-1").strip()
        self._field.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        part = self._current
        assert part is not None
        _, options = parse_options_header(part.headers.get("content-disposition"))
        raw_name = options.get(b"name")
        if raw_name is None:
            raise _unprocessable("Una parte del formulario no tiene nombre.")
        part.name = raw_name.decode("utf-8", errors="replace")
        if part.name in self.parts:
            raise _unprocessable(f"La parte «{part.name}» aparece más de una vez.")
        declared = part.headers.get("content-type")
        if declared is not None:
            part.content_type = parse_options_header(declared)[0].decode("latin-1").lower()
        if part.name == METADATA_PART:
            part.limit = MAX_METADATA_BYTES
        else:
            self._images += 1
            if self._images > self.max_images:
                raise _too_large(
                    f"La ráfaga trae más de {self.max_images} imágenes, el máximo permitido."
                )
            part.limit = self.max_image_bytes
        self.parts[part.name] = part

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._current
        assert part is not None
        if len(part.data) + (end - start) > part.limit:
            if part.name == METADATA_PART:
                raise _too_large("La parte «metadata» es demasiado grande.")
            raise _too_large(
                f"La imagen «{part.name}» supera el tamaño máximo de {part.limit} bytes."
            )
        part.data += data[start:end]

    def _end(self) -> None:
        self.ended = True


async def _read_burst(request: Request, server: ServerSettings) -> dict[str, _Part]:
    """The parts of the multipart body, read as it streams in under the configured limits."""
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise _unprocessable("El cuerpo debe ser multipart/form-data con su boundary.")
    total_cap = (
        server.max_capture_images * server.max_capture_image_bytes
        + MAX_METADATA_BYTES
        + _MULTIPART_OVERHEAD_BYTES
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > total_cap:
        raise _too_large("La ráfaga es más grande de lo que el servidor acepta.")

    reader = _BurstReader(server.max_capture_image_bytes, server.max_capture_images)
    try:
        parser = MultipartParser(boundary, reader.callbacks())
    except Exception as error:  # a boundary python-multipart refuses (too long, say)
        raise _unprocessable("El boundary del cuerpo multipart no es válido.") from error
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > total_cap:
                raise _too_large("La ráfaga es más grande de lo que el servidor acepta.")
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as error:
        raise _unprocessable("El cuerpo multipart está mal formado.") from error
    if not reader.ended:
        raise _unprocessable("El cuerpo multipart está incompleto.")
    return reader.parts


def _validate(parts: Mapping[str, _Part], server: ServerSettings) -> protocol.CaptureUploadRequest:
    """The burst's metadata, once every part matches what it declares."""
    metadata_part = parts.get(METADATA_PART)
    if metadata_part is None:
        raise _unprocessable("Falta la parte «metadata» con los datos de la captura.")
    try:
        metadata = protocol.CaptureUploadRequest.model_validate_json(bytes(metadata_part.data))
    except ValidationError as error:
        # Only where the problem is, never the value (the input is never echoed back).
        where = ", ".join(
            ".".join(str(item) for item in problem["loc"]) or "(raíz)" for problem in error.errors()
        )
        raise _unprocessable(
            f"La parte «metadata» no es una rest.sessions.captures.request válida ({where})."
        ) from error
    if metadata.trigger == "command" and metadata.command_id is None:
        raise _unprocessable("Una captura pedida por comando debe traer su «command_id».")
    if metadata.trigger != "command" and metadata.command_id is not None:
        raise _unprocessable("Solo una captura pedida por comando lleva «command_id».")
    if len(metadata.images) > server.max_capture_images:
        raise _too_large(
            f"La ráfaga declara más de {server.max_capture_images} imágenes, el máximo permitido."
        )
    named = [image.part for image in metadata.images]
    if len(set(named)) != len(named):
        raise _unprocessable("La parte «metadata» nombra la misma imagen más de una vez.")
    for image in metadata.images:
        part = parts.get(image.part)
        if part is None:
            raise _unprocessable(f"Falta la imagen «{image.part}» que declara «metadata».")
        if part.content_type != image.content_type:
            raise _unprocessable(
                f"La imagen «{image.part}» no llega como {image.content_type}, que es lo que"
                " declara «metadata»."
            )
        if not part.data:
            raise _unprocessable(f"La imagen «{image.part}» está vacía.")
    extra = sorted(set(parts) - set(named) - {METADATA_PART})
    if extra:
        raise _unprocessable(
            "Sobran partes que «metadata» no declara: " + ", ".join(f"«{p}»" for p in extra) + "."
        )
    return metadata


def current_source_context(session: Session) -> str:
    """The source kind the session's captures go to now: the `source` of its latest persisted
    `button` event whose `button` is `switch_source` (mapped to the vault's kind), else `notes`.

    Read from the session's `events.jsonl`, so it survives a backend restart. Blocking file I/O:
    call it from a worker thread.
    """
    context = DEFAULT_SOURCE_KIND
    for event in session.read_events():
        if event.kind != BUTTON_EVENT_KIND or event.payload.get("button") != SWITCH_SOURCE:
            continue
        kind = _SOURCE_KINDS.get(str(event.payload.get("source")))
        if kind is not None:
            context = kind
    return context


def _source_meta(
    session: Session, metadata: protocol.CaptureUploadRequest, source_context: str
) -> dict[str, Any]:
    first = metadata.images[0]
    meta: dict[str, Any] = {
        "capture_id": metadata.capture_id,
        "session": session.id,
        "captured_at": datetime.fromtimestamp(first.client_time_ms / 1000, tz=UTC),
        "trigger": metadata.trigger,
    }
    if metadata.command_id is not None:
        meta["command_id"] = metadata.command_id
    meta |= {
        "image_count": len(metadata.images),
        "width_px": first.width_px,
        "height_px": first.height_px,
        "source_context": source_context,
    }
    return meta


def _response(
    metadata: protocol.CaptureUploadRequest, session_id: str, state: str, image_count: int
) -> protocol.CaptureUploadResponse:
    return protocol.CaptureUploadResponse(
        capture_id=metadata.capture_id,
        session_id=session_id,
        status=state,  # type: ignore[arg-type]
        image_count=image_count,
        received_at_ms=time.time_ns() // 1_000_000,
    )


def captures_router() -> APIRouter:
    """The capture upload route; service, bus and limits are read from `app.state`."""
    router = APIRouter(prefix="/api")
    # One lock per session: the stored-ids check and the store happen as one step.
    locks: dict[str, asyncio.Lock] = {}

    @router.post(
        "/sessions/{session_id}/captures",
        status_code=status.HTTP_201_CREATED,
        response_model=protocol.CaptureUploadResponse,
        response_model_exclude_none=True,
        responses={200: {"model": protocol.CaptureUploadResponse, "description": "Duplicate"}},
    )
    async def upload_capture(request: Request, session_id: SessionId) -> JSONResponse:
        service: SessionService = request.app.state.sessions
        server: ServerSettings = request.app.state.server
        try:
            session = await service.require_active(session_id)
        except UnknownSessionError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except SessionConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        except VaultUnavailableError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

        try:
            parts = await _read_burst(request, server)
            metadata = _validate(parts, server)
        except _RefusedError as refusal:
            raise HTTPException(refusal.status_code, refusal.detail) from refusal

        async with locks.setdefault(session.id, asyncio.Lock()):
            stored, context = await asyncio.to_thread(_capture_state, session)
            previous = stored.get(metadata.capture_id)
            if previous is not None:
                count = previous.get("image_count")
                body = _response(
                    metadata,
                    session.id,
                    "duplicate",
                    count if isinstance(count, int) and count >= 1 else len(metadata.images),
                )
                return JSONResponse(body.model_dump(exclude_none=True), status.HTTP_200_OK)

            first = metadata.images[0]
            try:
                path = await asyncio.to_thread(
                    put_source,
                    session.vault,
                    session.subject_slug,
                    session.topic_slug,
                    context,
                    f"capture{_EXTENSIONS[first.content_type]}",
                    bytes(parts[first.part].data),
                    _source_meta(session, metadata, context),
                )
            except SecretRefused as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "La imagen parece contener una clave y no se ha guardado.",
                ) from error
            service.note_change()
            payload: dict[str, Any] = {CAPTURE_ID_KEY: metadata.capture_id}
            payload["trigger"] = metadata.trigger
            if metadata.command_id is not None:
                payload["command_id"] = metadata.command_id
            payload |= {
                "image_count": len(metadata.images),
                "client_time_ms": metadata.client_time_ms,
                "source_path": _relative(path, session.vault.path),
                "source_context": context,
            }
            try:
                await request.app.state.bus.publish(
                    session.id, CAPTURE_EVENT_KIND, "phone", payload
                )
            except (SessionNotAttachedError, SessionEndedError) as error:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"la sesión {session.id} terminó mientras llegaba la captura",
                ) from error
            body = _response(metadata, session.id, "stored", len(metadata.images))
            return JSONResponse(body.model_dump(exclude_none=True), status.HTTP_201_CREATED)

    return router


def _capture_state(session: Session) -> tuple[dict[str, Mapping[str, Any]], str]:
    """The session's stored captures and its current source context (blocking)."""
    return stored_captures(session), current_source_context(session)


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


__all__ = [
    "DEFAULT_SOURCE_KIND",
    "MAX_METADATA_BYTES",
    "METADATA_PART",
    "captures_router",
    "current_source_context",
]
