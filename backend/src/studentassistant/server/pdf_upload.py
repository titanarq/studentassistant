"""`POST /api/subjects/{subject_id}/topics/{topic_id}/sources/pdf`: a PDF, or a page range of it.

The web's way to do what `studentassistant import-pdf` does: the body is `multipart/form-data`
with one `file` part (the PDF; its `filename` names it, `documento.pdf` without one) and an
optional `pages` part holding the range as the student types it (`82-94`, `páginas 82 a 94`,
`pág. 7`; empty or absent keeps every page). The body is parsed as it streams in, never buffered
whole: a `file` part over `[sources] max_pdf_bytes` stops the read at once with 413, so what is
held in memory is bounded by that limit. A body that is not multipart, a missing or repeated
`file`, a part that is neither, or a malformed `pages` is 422.

The PDF is then handed to `studentassistant.sources.import_pdf` in a worker thread (imports of one
topic one at a time, so they never race for the next `page-NNN` number), and the vault's sync is
told a file was written (`SessionService.note_change`), so the background loop commits and pushes
it. The refusals of `import_pdf` keep its Spanish message: too large (the file, the range's page
count or the kept pages) is 413; unreadable, password-protected, a range outside the PDF, or a
PDF that looks like it carries a key is 422. An unknown subject or topic is 404 (checked before
the body is read), a vault that cannot be opened 503. Every refusal (`{"detail": "..."}`) stores
nothing. A stored import answers 201 `PdfImportResponse`.

The route needs no active session: a PDF is a topic's source, not a session's.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from studentassistant.config import SourcesSettings
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.sources import (
    ImportedPdf,
    PageRange,
    PdfImportError,
    PdfTooLargeError,
    import_pdf,
    parse_page_range,
)
from studentassistant.vault import (
    SecretRefused,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
)

SubjectId = Annotated[str, PathParam(pattern=ID_PATTERN)]
TopicId = Annotated[str, PathParam(pattern=ID_PATTERN)]

FILE_PART = "file"
PAGES_PART = "pages"
DEFAULT_FILE_NAME = "documento.pdf"
MAX_PAGES_BYTES = 256
"""The largest `pages` part accepted: a range is a few characters."""

_MULTIPART_OVERHEAD_BYTES = 64 * 1024
"""Room for boundaries, part headers and the `pages` part on top of the PDF, in the total cap."""

UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
SECRET_DETAIL = "El PDF parece contener una clave o un token y no se ha guardado."


class PdfImportResponse(BaseModel):
    """201 of the PDF upload: what was stored and which pages of the original it keeps."""

    subject_id: str
    topic_id: str
    source_id: str = Field(description="`sources/pdf/page-NNN.pdf`, as provenance cites it.")
    vault_id: str = Field(description="The stored PDF's vault-relative path.")
    original_name: str
    original_page_count: int
    first_page: int = Field(description="First kept page, in the original's numbering.")
    last_page: int = Field(description="Last kept page, in the original's numbering.")
    page_count: int = Field(description="Pages of the stored PDF.")
    pages_without_text: list[int] = Field(
        description="Kept pages (original numbering) with no extractable text, e.g. scanned."
    )


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


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _file_too_large(max_bytes: int) -> _RefusedError:
    return _too_large(f"El PDF supera el máximo que se importa ({_megabytes(max_bytes)}).")


@dataclass
class _Part:
    name: str
    file_name: str | None
    limit: int
    data: bytearray = field(default_factory=bytearray)


@dataclass
class _UploadReader:
    """Streaming multipart callbacks that accept a `file` and a `pages` part within limits."""

    max_pdf_bytes: int
    parts: dict[str, _Part] = field(default_factory=dict)
    ended: bool = False
    _current: _Part | None = None
    _headers: dict[str, str] = field(default_factory=dict)
    _field: bytearray = field(default_factory=bytearray)
    _value: bytearray = field(default_factory=bytearray)

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
        self._current = None
        self._headers = {}

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        name = self._field.decode("latin-1").strip().lower()
        self._headers[name] = self._value.decode("latin-1").strip()
        self._field.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get("content-disposition"))
        raw_name = options.get(b"name")
        if raw_name is None:
            raise _unprocessable("Una parte del formulario no tiene nombre.")
        name = raw_name.decode("utf-8", errors="replace")
        if name not in (FILE_PART, PAGES_PART):
            raise _unprocessable(
                f"Sobra la parte «{name}»: solo se aceptan «{FILE_PART}» y «{PAGES_PART}»."
            )
        if name in self.parts:
            raise _unprocessable(f"La parte «{name}» aparece más de una vez.")
        raw_file_name = options.get(b"filename")
        # Browsers send the file name as raw UTF-8 bytes; the header was decoded as latin-1.
        file_name = (
            raw_file_name.decode("latin-1").encode("latin-1").decode("utf-8", errors="replace")
            if raw_file_name is not None
            else None
        )
        limit = self.max_pdf_bytes if name == FILE_PART else MAX_PAGES_BYTES
        self._current = _Part(name=name, file_name=file_name, limit=limit)
        self.parts[name] = self._current

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._current
        assert part is not None
        if len(part.data) + (end - start) > part.limit:
            if part.name == PAGES_PART:
                raise _too_large("La parte «pages» es demasiado grande.")
            raise _file_too_large(part.limit)
        part.data += data[start:end]

    def _end(self) -> None:
        self.ended = True


async def _read_upload(request: Request, max_pdf_bytes: int) -> dict[str, _Part]:
    """The parts of the multipart body, read as it streams in under the configured limit."""
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise _unprocessable("El cuerpo debe ser multipart/form-data con su boundary.")
    total_cap = max_pdf_bytes + _MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > total_cap:
        raise _file_too_large(max_pdf_bytes)

    reader = _UploadReader(max_pdf_bytes)
    try:
        parser = MultipartParser(boundary, reader.callbacks())
    except Exception as error:  # a boundary python-multipart refuses (too long, say)
        raise _unprocessable("El boundary del cuerpo multipart no es válido.") from error
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > total_cap:
                raise _file_too_large(max_pdf_bytes)
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as error:
        raise _unprocessable("El cuerpo multipart está mal formado.") from error
    if not reader.ended:
        raise _unprocessable("El cuerpo multipart está incompleto.")
    return reader.parts


def _validate(parts: dict[str, _Part]) -> tuple[str, bytes, PageRange | None]:
    """The PDF's name, its bytes and the page range asked for (`None`: every page)."""
    file_part = parts.get(FILE_PART)
    if file_part is None:
        raise _unprocessable("Falta la parte «file» con el PDF.")
    if not file_part.data:
        raise _unprocessable("El PDF está vacío.")
    name = Path((file_part.file_name or "").replace("\\", "/")).name or DEFAULT_FILE_NAME
    pages_part = parts.get(PAGES_PART)
    text = bytes(pages_part.data).decode("utf-8", errors="replace") if pages_part else ""
    try:
        pages = parse_page_range(text) if text.strip() else None
    except PdfImportError as error:
        raise _unprocessable(str(error)) from error
    return name, bytes(file_part.data), pages


def _response(
    subject_id: str, topic_id: str, vault: Vault, imported: ImportedPdf
) -> PdfImportResponse:
    meta = imported.meta
    return PdfImportResponse(
        subject_id=subject_id,
        topic_id=topic_id,
        source_id=imported.source_id,
        vault_id=imported.path.resolve().relative_to(vault.path.resolve()).as_posix(),
        original_name=meta["original_name"],
        original_page_count=meta["original_page_count"],
        first_page=meta["first_page"],
        last_page=meta["last_page"],
        page_count=meta["page_count"],
        pages_without_text=[page.original_page for page in imported.pages if not page.has_text],
    )


def pdf_upload_router() -> APIRouter:
    """The PDF upload route; the service and the `[sources]` limits are read from `app.state`."""
    router = APIRouter(prefix="/api")
    # One lock per topic: `put_source` numbers `page-NNN` from what the directory holds.
    locks: dict[tuple[str, str], asyncio.Lock] = {}

    @router.post(
        "/subjects/{subject_id}/topics/{topic_id}/sources/pdf",
        status_code=status.HTTP_201_CREATED,
        responses={
            404: {"description": "Unknown subject or topic."},
            413: {"description": "The PDF, its range or its kept pages are too large."},
            422: {"description": "Not a readable PDF, a bad range, or a malformed body."},
            503: {"description": "The vault cannot be opened."},
        },
    )
    async def upload_pdf(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> PdfImportResponse:
        service: SessionService = request.app.state.sessions
        limits: SourcesSettings = request.app.state.sources
        try:
            vault = await service.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error

        try:
            parts = await _read_upload(request, limits.max_pdf_bytes)
            name, content, pages = _validate(parts)
        except _RefusedError as refusal:
            raise HTTPException(refusal.status_code, refusal.detail) from refusal
        del parts  # `content` is the only copy kept while the import runs

        async with locks.setdefault((subject_id, topic_id), asyncio.Lock()):
            try:
                imported = await asyncio.to_thread(
                    import_pdf,
                    vault,
                    subject_id,
                    topic_id,
                    name,
                    content,
                    pages=pages,
                    settings=limits,
                )
            except PdfTooLargeError as error:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(error)) from error
            except PdfImportError as error:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
            except SecretRefused as error:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SECRET_DETAIL) from error
            except (SubjectNotFoundError, TopicNotFoundError) as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        service.note_change()
        return _response(subject_id, topic_id, vault, imported)

    return router


__all__ = [
    "FILE_PART",
    "PAGES_PART",
    "PdfImportResponse",
    "pdf_upload_router",
]
