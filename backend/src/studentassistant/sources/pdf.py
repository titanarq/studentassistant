"""PDF import: a teacher's PDF, or a page range of it, stored as a topic source with its pages.

`import_pdf` keeps only the pages asked for ("páginas 82-94"), as a new PDF, and stores it through
`vault.put_source` under `sources/pdf/page-NNN.pdf` together with, per kept page `K` (1-based in
the stored file), its text extracted by PyMuPDF (`page-NNN.pKKK.txt`) and a rendered thumbnail
(`page-NNN.pKKK.jpg`) the web shows next to a citation. The sidecar `page-NNN.yaml` records the
original file name, its SHA-256 and page count, the kept range in the original's numbering
(`first_page`, `last_page`) and `page_count`.

Page numbers: a provenance footnote cites `sources/pdf/page-NNN.pdf#page=K` with `K` counted in
the *stored* file, because that is what the link opens on GitHub and what Claude's citations
(`page_location.start_page_number`) count in; `original_page(meta, K)` gives the page the student
knows (`first_page + K - 1`).

Size policy (no Git LFS, ADR-0002): a PDF over `[sources] max_pdf_bytes` is refused before it is
opened; a range of more than `max_pdf_pages` pages is refused (never truncated); a kept PDF over
`max_stored_pdf_bytes` is refused, and the message asks for a narrower range. Encrypted and
unreadable files are refused. A refused import leaves nothing in the vault.

For the editor, `pdf_document_block` turns a stored PDF into a Claude `document` block (base64,
citations enabled) and `citation_source_id` maps a citation Claude returns back to the source id
the notes cite. Everything here is CPU-bound: a server caller runs it in a worker thread.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pymupdf

from studentassistant.config import SourcesSettings
from studentassistant.vault import (
    SourceError,
    Vault,
    put_source,
    read_source,
    sources_directory,
)

PDF_KIND = "pdf"
PDF_MEDIA_TYPE = "application/pdf"
_PDF_SOURCE_ID = re.compile(r"^sources/pdf/(?P<name>page-\d{3,}\.pdf)(?:#page=(?P<page>\d+))?$")
_RANGE = re.compile(r"^(?:p[aá]g[a-z]*\.?\s*)?(\d+)(?:\s*(?:-|–|—|a|al|hasta)\s*(\d+))?$")


class PdfImportError(Exception):
    """A PDF this backend refuses to import; the message is Spanish, for the student."""


class PdfTooLargeError(PdfImportError):
    """The PDF, the page range or the kept pages exceed a `[sources]` limit."""


class PdfUnreadableError(PdfImportError):
    """Not a PDF PyMuPDF can open, or one that is encrypted."""


class PageRangeError(PdfImportError):
    """A page range that is malformed or falls outside the PDF."""


@dataclass(frozen=True)
class PageRange:
    """Pages `first`..`last` of the original PDF, both included, counted from 1."""

    first: int
    last: int

    def __post_init__(self) -> None:
        if self.first < 1 or self.last < self.first:
            raise PageRangeError(
                f"«{self.first}-{self.last}» no es un rango de páginas válido:"
                " la primera página es la 1 y el rango no puede ir hacia atrás."
            )

    @property
    def count(self) -> int:
        return self.last - self.first + 1


def parse_page_range(text: str) -> PageRange:
    """`"82-94"`, `"82–94"`, `"páginas 82 a 94"`, `"82"` -> a `PageRange`.

    Raises:
        PageRangeError: when `text` is not a page or a range of pages.
    """
    match = _RANGE.match(text.strip().lower())
    if match is None:
        raise PageRangeError(f"«{text}» no es un rango de páginas: escríbelo como 82-94.")
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) is not None else first
    return PageRange(first, last)


@dataclass(frozen=True)
class ImportedPage:
    """One kept page: `page` in the stored file, `original_page` in the original, its files."""

    page: int
    original_page: int
    source_id: str
    text_path: Path
    image_path: Path
    has_text: bool


@dataclass(frozen=True)
class ImportedPdf:
    """What `import_pdf` stored: the PDF's path, its topic-relative id, sidecar and pages."""

    path: Path
    source_id: str
    meta: dict[str, Any]
    pages: tuple[ImportedPage, ...]


def import_pdf(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    name: str,
    content: bytes,
    *,
    pages: PageRange | None = None,
    settings: SourcesSettings | None = None,
    now: Callable[[], datetime] | None = None,
) -> ImportedPdf:
    """Store `content` (a PDF called `name`), or only its `pages`, as a source of the topic.

    Without `pages` every page is kept. See the module docstring for the files written and the
    size policy.

    Raises:
        PdfTooLargeError, PdfUnreadableError, PageRangeError: the import is refused (Spanish
            message); nothing is written.
        SecretRefused: the PDF or a page's text looks like it carries a key; nothing is written.
        SubjectNotFoundError, TopicNotFoundError (and the vault's other read errors): the topic
            is not one this backend can read.
    """
    limits = settings or SourcesSettings()
    if len(content) > limits.max_pdf_bytes:
        raise PdfTooLargeError(
            f"El PDF «{name}» ocupa {_megabytes(len(content))} y el máximo que se importa es"
            f" {_megabytes(limits.max_pdf_bytes)}."
        )
    original = _open(name, content)
    try:
        total = original.page_count
        kept = pages or PageRange(1, total)
        if kept.last > total:
            raise PageRangeError(
                f"El PDF «{name}» tiene {total} páginas: el rango {kept.first}-{kept.last}"
                " se sale de él."
            )
        if kept.count > limits.max_pdf_pages:
            raise PdfTooLargeError(
                f"El rango {kept.first}-{kept.last} tiene {kept.count} páginas y el máximo por"
                f" importación es {limits.max_pdf_pages}: elige un rango más corto."
            )
        subset = pymupdf.open()
        try:
            subset.insert_pdf(original, from_page=kept.first - 1, to_page=kept.last - 1)
            stored = subset.tobytes(garbage=3, deflate=True)
            derived: dict[str, bytes | str] = {}
            texts: list[str] = []
            for index, page in enumerate(subset, start=1):
                text = page.get_text("text")
                texts.append(text)
                derived[f"p{index:03d}.txt"] = text
                derived[f"p{index:03d}.jpg"] = _thumbnail(page, limits)
        finally:
            subset.close()
    finally:
        original.close()
    if len(stored) > limits.max_stored_pdf_bytes:
        raise PdfTooLargeError(
            f"Las páginas {kept.first}-{kept.last} de «{name}» ocupan {_megabytes(len(stored))}"
            f" y el máximo que se guarda es {_megabytes(limits.max_stored_pdf_bytes)}:"
            " elige un rango más corto."
        )
    meta: dict[str, Any] = {
        "original_name": PurePosixPath(name.replace("\\", "/")).name,
        "imported_at": (now or _utc_now)(),
        "original_sha256": hashlib.sha256(content).hexdigest(),
        "original_page_count": total,
        "first_page": kept.first,
        "last_page": kept.last,
        "page_count": kept.count,
    }
    path = put_source(
        vault, subject_slug, topic_slug, PDF_KIND, "document.pdf", stored, meta, derived=derived
    )
    source_id = f"sources/{PDF_KIND}/{path.name}"
    imported = tuple(
        ImportedPage(
            page=index,
            original_page=kept.first + index - 1,
            source_id=f"{source_id}#page={index}",
            text_path=path.with_name(f"{path.stem}.p{index:03d}.txt"),
            image_path=path.with_name(f"{path.stem}.p{index:03d}.jpg"),
            has_text=bool(text.strip()),
        )
        for index, text in enumerate(texts, start=1)
    )
    return ImportedPdf(path=path, source_id=source_id, meta=meta, pages=imported)


def original_page(meta: Mapping[str, Any], page: int) -> int:
    """The original PDF's number of page `page` of a stored PDF, from its sidecar `meta`."""
    return int(meta.get("first_page", 1)) + page - 1


def pdf_page_count(vault: Vault, subject_slug: str, topic_slug: str, source_id: str) -> int | None:
    """How many pages the stored PDF `source_id` (`sources/pdf/page-NNN.pdf`, any `#page=` part
    ignored) has, from its sidecar; `None` when it is not a stored PDF of the topic."""
    match = _PDF_SOURCE_ID.match(source_id)
    if match is None:
        return None
    try:
        stored = read_source(vault, _vault_relative(vault, subject_slug, topic_slug, match["name"]))
    except SourceError:
        return None
    count = (stored.meta or {}).get("page_count")
    return count if isinstance(count, int) and count >= 1 else None


def pdf_has_page(vault: Vault, subject_slug: str, topic_slug: str, source_id: str) -> bool:
    """Whether `source_id` names a stored PDF of the topic and, with `#page=K`, one of its pages.

    This is what lets the provenance validator resolve `sources/pdf/page-001.pdf#page=3`.
    """
    match = _PDF_SOURCE_ID.match(source_id)
    if match is None:
        return False
    count = pdf_page_count(vault, subject_slug, topic_slug, source_id)
    if count is None:
        return False
    page = match["page"]
    return page is None or 1 <= int(page) <= count


def pdf_document_block(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    source_id: str,
    *,
    cache: bool = False,
) -> dict[str, Any]:
    """The stored PDF `source_id` as a Claude `document` content block with citations enabled.

    The block carries the PDF base64-encoded, the original file name as `title` and, as
    `context`, which original pages it holds, so the editor can speak of "página 84" while its
    citations count pages of this document. `cache=True` marks it as a cache breakpoint. The block
    goes into a user message before the text that asks about it; `citation_source_id` maps the
    citations of the answer back to source ids.

    Raises:
        PdfImportError: when `source_id` is not a stored PDF of the topic.
    """
    match = _PDF_SOURCE_ID.match(source_id)
    if match is None or match["page"] is not None:
        raise PdfImportError(f"«{source_id}» no es un PDF guardado del tema.")
    try:
        stored = read_source(vault, _vault_relative(vault, subject_slug, topic_slug, match["name"]))
    except SourceError as error:
        raise PdfImportError(f"«{source_id}» no es un PDF guardado del tema.") from error
    meta = stored.meta or {}
    first, last = meta.get("first_page"), meta.get("last_page")
    title = str(meta.get("original_name") or match["name"])
    block: dict[str, Any] = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": PDF_MEDIA_TYPE,
            "data": base64.b64encode(stored.content).decode("ascii"),
        },
        "title": title,
        "citations": {"enabled": True},
    }
    if isinstance(first, int) and isinstance(last, int):
        block["context"] = (
            f"Fuente {source_id}: páginas {first}-{last} del PDF «{title}». La página 1 de este"
            f" documento es la página {first} del original."
        )
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def citation_source_id(source_id: str, citation: Mapping[str, Any]) -> str | None:
    """The provenance source id (`sources/pdf/page-001.pdf#page=3`) of one Claude citation.

    `citation` is an entry of a text block's `citations` whose `document_index` points at the
    block made from `source_id`; only a `page_location` citation maps (to its first page), any
    other kind gives `None`.
    """
    if citation.get("type") != "page_location":
        return None
    page = citation.get("start_page_number")
    if not isinstance(page, int) or page < 1:
        return None
    return f"{source_id.split('#', 1)[0]}#page={page}"


# -- helpers ---------------------------------------------------------------------------------------


def _open(name: str, content: bytes) -> pymupdf.Document:
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (RuntimeError, ValueError) as error:
        raise PdfUnreadableError(f"«{name}» no es un PDF que se pueda leer.") from error
    if not document.is_pdf or document.page_count < 1:
        document.close()
        raise PdfUnreadableError(f"«{name}» no es un PDF que se pueda leer.")
    if document.needs_pass or document.is_encrypted:
        document.close()
        raise PdfUnreadableError(
            f"«{name}» está protegido con contraseña: quítasela antes de importarlo."
        )
    return document


def _thumbnail(page: pymupdf.Page, limits: SourcesSettings) -> bytes:
    """The page rendered as a JPEG whose long edge is `pdf_thumbnail_long_edge` pixels."""
    rect = page.rect
    zoom = limits.pdf_thumbnail_long_edge / max(rect.width, rect.height, 1.0)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pixmap.tobytes("jpeg", jpg_quality=limits.pdf_thumbnail_quality)


def _vault_relative(vault: Vault, subject_slug: str, topic_slug: str, file_name: str) -> str:
    directory = sources_directory(vault, subject_slug, topic_slug, PDF_KIND)
    return (directory / file_name).relative_to(vault.path).as_posix()


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _utc_now() -> datetime:
    return datetime.now(UTC)
