"""The student editing the document: save the whole notes, and paste an image into them.

Thin: the work is `studentassistant.editor.direct_edit` and `vault.put_pasted_image`. Every route
opens the vault through the `SessionService`, like the other notes routes; none calls Claude, so
they work without an `llm_transport`.

- `PUT /api/subjects/{s}/topics/{t}/notes` (`StudentSaveRequest`: `text`, `base_revision` -- the
  `revision` of the notes the student started from, as `GET .../notes` gave it; `null` when the
  topic had none) -> `StudentEditResult` (the new `revision`, `commit`, `diff`, the normalised
  `notes`, `changed_sections`, `normalised`). The save does not wait for an editor chat turn of
  the topic: both write under the short write lock of `editor.notes_lock`, and the turn re-asks
  the editor on the new notes. It is refused with `409 notes_busy` while anything else holds the
  topic's notes lock (`NotesGenerator.claim`): "prepárame el tema", a restore, the doubts, an
  undo. `notes.edited` (origin `user`) is published on the bus when the topic's session is the
  active one.
- `POST /api/subjects/{s}/topics/{t}/sources/images`: `multipart/form-data` with one `file` part,
  a PNG, JPEG or WebP image (told by its bytes, not its name) of at most `[sources]
  max_pasted_image_bytes` -> 201 `PastedImage` (`source_id` `sources/images/img-NNN.<ext>`, the
  vault-relative `path` and the `markdown` to insert, `![Imagen pegada N](../sources/images/...)`).

Errors, as `{"detail": "..."}` in Spanish: a vault that cannot be opened 503, an unknown topic
404; a stale `base_revision` 409 with code `notes_changed` and the current notes' `text` and
`revision` in the body; `notes_busy` 409; notes that break the format even after normalising 422
with the Spanish `errors` list; an image too large 413, not an image, empty or malformed 422.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from studentassistant.config import SourcesSettings
from studentassistant.editor.direct_edit import (
    NotesChangedError,
    StudentEditInvalidError,
    StudentEditResult,
    save_student_edit,
)
from studentassistant.editor.notes_format import IMAGE_TEXT, LINK_PREFIX
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import ApiError, caller_speaks_error_codes
from studentassistant.server.notes_routes import TURN_HOLDER, NotesGenerator
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    GitSync,
    SecretRefused,
    SourceError,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
    put_pasted_image,
    topic_directory,
)

SubjectId = Annotated[str, PathParam(pattern=ID_PATTERN)]
TopicId = Annotated[str, PathParam(pattern=ID_PATTERN)]

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = (
    "Ahora mismo el editor está reescribiendo los apuntes de este tema (preparándolos,"
    " restaurando una versión o con las dudas): espera a que termine y vuelve a guardar."
)
INVALID_DETAIL = "Los apuntes no cumplen el formato"
NOT_AN_IMAGE_DETAIL = "El archivo no es una imagen PNG, JPEG o WebP."
EMPTY_IMAGE_DETAIL = "La imagen está vacía."
SECRET_DETAIL = "La imagen parece contener una clave o un token y no se ha guardado."
FILE_PART = "file"
MAX_NOTES_BODY_CHARS = 400_000
_MULTIPART_OVERHEAD_BYTES = 16 * 1024


class StudentSaveRequest(BaseModel):
    """The whole `apuntes.md` the student ended up with and the revision they started from."""

    text: str = Field(max_length=MAX_NOTES_BODY_CHARS)
    base_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="The `revision` of the notes the edit started from; `null`: there were none.",
    )


class PastedImage(BaseModel):
    """201 of the image upload: the stored source and the Markdown that shows it in the notes."""

    source_id: str = Field(description="`sources/images/img-NNN.<ext>`, as provenance cites it.")
    path: str = Field(description="The stored image's vault-relative path.")
    markdown: str = Field(description="`![Imagen pegada N](../sources/images/img-NNN.<ext>)`.")


def _image_type(content: bytes) -> str | None:
    """The media type of an image told by its first bytes: PNG, JPEG or WebP; else `None`."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


class _RefusedError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _too_large(max_bytes: int) -> _RefusedError:
    size = f"{max_bytes / (1024 * 1024):.1f} MB"
    return _RefusedError(status.HTTP_413_CONTENT_TOO_LARGE, f"La imagen supera el máximo ({size}).")


def _unprocessable(detail: str) -> _RefusedError:
    return _RefusedError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def _read_image(request: Request, max_bytes: int) -> bytes:
    """The bytes of the one `file` part, read as the body streams in under `max_bytes`."""
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise _unprocessable("El cuerpo debe ser multipart/form-data con su boundary.")
    total_cap = max_bytes + _MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > total_cap:
        raise _too_large(max_bytes)

    parts: dict[str, bytearray] = {}
    state: dict[str, Any] = {"headers": {}, "field": bytearray(), "value": bytearray()}
    current: list[bytearray | None] = [None]
    ended = [False]

    def on_part_begin() -> None:
        state["headers"] = {}
        current[0] = None

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["value"] += data[start:end]

    def on_header_end() -> None:
        name = bytes(state["field"]).decode("latin-1").strip().lower()
        state["headers"][name] = bytes(state["value"]).decode("latin-1").strip()
        state["field"].clear()
        state["value"].clear()

    def on_headers_finished() -> None:
        _, disposition = parse_options_header(state["headers"].get("content-disposition"))
        raw_name = disposition.get(b"name")
        name = raw_name.decode("utf-8", errors="replace") if raw_name is not None else ""
        if name != FILE_PART:
            raise _unprocessable(f"Solo se acepta una parte «{FILE_PART}» con la imagen.")
        if name in parts:
            raise _unprocessable(f"La parte «{FILE_PART}» aparece más de una vez.")
        parts[name] = bytearray()
        current[0] = parts[name]

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part = current[0]
        assert part is not None
        if len(part) + (end - start) > max_bytes:
            raise _too_large(max_bytes)
        part += data[start:end]

    def on_end() -> None:
        ended[0] = True

    try:
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": on_part_begin,
                "on_header_field": on_header_field,
                "on_header_value": on_header_value,
                "on_header_end": on_header_end,
                "on_headers_finished": on_headers_finished,
                "on_part_data": on_part_data,
                "on_end": on_end,
            },
        )
    except Exception as error:  # a boundary python-multipart refuses
        raise _unprocessable("El boundary del cuerpo multipart no es válido.") from error
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > total_cap:
                raise _too_large(max_bytes)
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as error:
        raise _unprocessable("El cuerpo multipart está mal formado.") from error
    if not ended[0] or FILE_PART not in parts:
        raise _unprocessable(f"Falta la parte «{FILE_PART}» con la imagen.")
    return bytes(parts[FILE_PART])


def notes_edit_router() -> APIRouter:
    """The student's save of the notes and the pasted-image upload."""
    router = APIRouter(prefix="/api")

    async def open_topic(request: Request, subject_id: str, topic_id: str) -> tuple[Vault, GitSync]:
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        sync = sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL)
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        return vault, sync

    def publisher(request: Request, subject_id: str, topic_id: str) -> Any:
        sessions: SessionService = request.app.state.sessions

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await sessions.bus.publish(active.session_id, kind, "user", payload)

        return publish

    @router.put(
        "/subjects/{subject_id}/topics/{topic_id}/notes",
        responses={
            404: {"description": "Unknown topic."},
            409: {"description": "`notes_changed` (stale base revision) or `notes_busy`."},
            422: {"description": "The notes break the format even after normalising."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def save_notes(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: StudentSaveRequest
    ) -> StudentEditResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        generator: NotesGenerator | None = request.app.state.notes
        if generator is not None and generator.holder(subject_id, topic_id) not in (
            None,
            TURN_HOLDER,
        ):
            raise ApiError(409, BUSY_DETAIL, ErrorCode.NOTES_BUSY)
        try:
            return await save_student_edit(
                vault,
                subject_id,
                topic_id,
                body.text,
                body.base_revision,
                sync=sync,
                on_event=publisher(request, subject_id, topic_id),
            )
        except NotesChangedError as error:
            content: dict[str, Any] = {
                "detail": str(error),
                "text": error.text,
                "revision": error.revision,
            }
            if caller_speaks_error_codes(request):
                content["code"] = ErrorCode.NOTES_CHANGED.value
            return JSONResponse(status_code=409, content=content)  # type: ignore[return-value]
        except StudentEditInvalidError as error:
            return JSONResponse(  # type: ignore[return-value]
                status_code=422,
                content={"detail": f"{INVALID_DETAIL}: {error}", "errors": error.errors},
            )
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error

    @router.post(
        "/subjects/{subject_id}/topics/{topic_id}/sources/images",
        status_code=status.HTTP_201_CREATED,
        responses={
            404: {"description": "Unknown subject or topic."},
            413: {"description": "The image is too large."},
            422: {"description": "Not a PNG, JPEG or WebP image, or a malformed body."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def paste_image(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> PastedImage:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        limits: SourcesSettings = request.app.state.sources
        try:
            content = await _read_image(request, limits.max_pasted_image_bytes)
            if not content:
                raise _unprocessable(EMPTY_IMAGE_DETAIL)
            media_type = _image_type(content)
            if media_type is None:
                raise _unprocessable(NOT_AN_IMAGE_DETAIL)
        except _RefusedError as refusal:
            raise HTTPException(refusal.status_code, refusal.detail) from refusal
        try:
            path = await asyncio.to_thread(
                put_pasted_image, vault, subject_id, topic_id, content, media_type
            )
        except SecretRefused as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SECRET_DETAIL) from error
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        except SourceError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, NOT_AN_IMAGE_DETAIL
            ) from error
        request.app.state.sessions.note_change()
        topic_root = topic_directory(vault, subject_id, topic_id)
        source_id = path.relative_to(topic_root).as_posix()
        number = int(path.name.split(".", 1)[0].removeprefix("img-"))
        return PastedImage(
            source_id=source_id,
            path=path.relative_to(vault.path).as_posix(),
            markdown=f"![{IMAGE_TEXT} {number}]({LINK_PREFIX}{source_id})",
        )

    return router


__all__ = ["PastedImage", "StudentSaveRequest", "notes_edit_router"]
