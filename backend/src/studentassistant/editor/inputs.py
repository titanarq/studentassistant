"""The editor's input for writing a topic's notes: everything captured, stable parts first.

`assemble_input` reads one topic through the vault's public readers (and the observer's folded
state) and lays it out for one `editor` call:

- **System** (cached by the llm client): the `editor_generate` prompt, then the topic block --
  subject, topic title, fidelity mode and the subject's style guide. It changes only when the
  prompt, the style guide or the mode does.
- **First user message**, in this order:
  1. the catalogue of citable sources, each with the exact footnote definition to cite it;
  2. the sources: every notes and book page (its transcription `page-NNN.md`, plus the page image
     when there is no transcription, it holds a scheme or an uncertain word), every stored PDF (a
     `document` block, or its extracted page texts past the attachment budget) and every web
     snapshot -- with a cache breakpoint on the last of them, since sources only grow;
  3. the transcript grouped by the observer's outline (each line with its session and span, the
     pages linked to the section, its concepts), the observer's remarks, the pending items (open
     ones are doubts, resolved ones decisions), the topic digest and the current notes, if any;
  4. the instruction, with a second breakpoint so that a re-ask re-reads all of it from the cache.

Attachments are bounded: at most `max_page_images` page images (the API refuses more than 20
images of this size in one request) and `max_attachment_bytes` of base64 in total (requests are
capped at 32 MB). What does not fit is sent as text only and listed in `EditorInput.omitted`.

`record_content` is the same message with every image and document block replaced by a short
reference to its source, which is what the conversation file keeps: the sources are in the vault
already. Nothing here writes the vault or calls Claude.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from studentassistant.editor.notes_format import IA_TEXT, FidelityMode, parse_provenance
from studentassistant.editor.notes_format import FootnoteDefinition as _Definition
from studentassistant.llm import Prompt
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    SEGMENT_EVENT_KIND,
    PendingItem,
    TopicState,
    load_observer_snapshot,
)
from studentassistant.sources import original_page, pdf_document_block
from studentassistant.vault import (
    SourceError,
    StoredSource,
    Vault,
    get_subject,
    get_topic,
    list_sources,
    read_notes,
    read_source,
    read_topic_events,
    topic_directory,
)

DigestReader = Callable[[Vault, str, str], str | None]
"""Reads a topic's digest (`state/digest.md`, written by the observer, #56); `None` if none."""

MAX_PAGE_IMAGES = 20
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024
FIDELITY_MODES: tuple[FidelityMode, ...] = ("estricto", "ampliado")
DEFAULT_FIDELITY: FidelityMode = "estricto"

IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
PAGE_KIND_TEXT = {"notes": "Apuntes", "book": "Libro"}

# A transcription holds a scheme when it has a mermaid block, nested list items or arrows; an
# uncertain word is `[[?palabra]]`, an illegible one `[[?]]` (the page transcription, #50).
_UNCERTAIN = re.compile(r"\[\[\?[^\]]*\]\]")
_SCHEME = re.compile(r"```mermaid|^[ \t]{2,}(?:[-*+]|\d+[.)])[ \t]|->|→|⇒", re.MULTILINE)
_PAGE_NAME = re.compile(r"^page-(?P<number>\d{3,})\.(?P<ext>[A-Za-z0-9]+)$")
LOW_CONFIDENCE = 0.8


@dataclass(frozen=True)
class CitableSource:
    """One source the notes may cite, with the footnote definition text that cites it."""

    source_id: str
    kind: str
    definition: str  # `[texto](enlace)`, what follows `[^label]: `
    description: str = ""


@dataclass
class EditorInput:
    """One topic laid out for the editor: see the module docstring."""

    subject_slug: str
    topic_slug: str
    subject_name: str
    topic_title: str
    fidelity_mode: FidelityMode
    system: list[str]
    content: list[dict[str, Any]]
    record_content: list[dict[str, Any]]
    catalogue: list[CitableSource] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    previous_notes: bool = False
    has_digest: bool = False

    def summary(self) -> dict[str, Any]:
        """What the conversation file's `context` record says about this input."""
        return {
            "fidelity_mode": self.fidelity_mode,
            "sources": [source.source_id for source in self.catalogue],
            "sessions": list(self.sessions),
            "images": list(self.images),
            "documents": list(self.documents),
            "omitted": list(self.omitted),
            "previous_notes": self.previous_notes,
            "digest": self.has_digest,
        }


# -- the transcript and the observer's outline ---------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    session_id: str
    segment_id: str
    start_ms: int
    end_ms: int
    text: str

    @property
    def span(self) -> str:
        start = math.floor(self.start_ms / 1000)
        end = max(start, math.ceil(self.end_ms / 1000))
        return f"{_clock(start)}-{_clock(end)}"

    def line(self) -> str:
        return f"- [{self.session_id} t={self.span}] {self.text.strip()}"


def _clock(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@dataclass
class _Log:
    """What the editor needs of the event logs: segments in order, and each capture's page."""

    segments: list[_Segment] = field(default_factory=list)
    capture_pages: dict[str, str] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)


def _read_log(vault: Vault, subject_slug: str, topic_slug: str) -> _Log:
    log = _Log()
    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    for session_id, event in read_topic_events(vault, subject_slug, topic_slug):
        if not log.sessions or log.sessions[-1] != session_id:
            log.sessions.append(session_id)
        payload = event.payload
        if event.kind == SEGMENT_EVENT_KIND:
            text = payload.get("text")
            segment_id = payload.get("segment_id")
            if not isinstance(text, str) or not text.strip() or not isinstance(segment_id, str):
                continue
            start = _int(payload.get("session_start_ms"), event.t)
            end = max(start, _int(payload.get("session_end_ms"), start))
            log.segments.append(_Segment(session_id, segment_id, start, end, text))
        elif event.kind == CAPTURE_EVENT_KIND:
            capture_id, path = payload.get("capture_id"), payload.get("source_path")
            if isinstance(capture_id, str) and isinstance(path, str):
                log.capture_pages.setdefault(capture_id, path.removeprefix(prefix + "/"))
    return log


def _render_transcript(state: TopicState, log: _Log) -> str:
    """The transcript grouped by the observer's outline, unassigned segments last."""
    by_section: dict[str | None, list[_Segment]] = {}
    for segment in log.segments:
        section = state.assignments.get(segment.segment_id)
        key = section if section in state.sections else None
        by_section.setdefault(key, []).append(segment)
    pages_of: dict[str, list[str]] = {}
    for capture_id, segment_ids in state.capture_links.items():
        page = log.capture_pages.get(capture_id)
        if page is None:
            continue
        for section in {state.assignments.get(s) for s in segment_ids}:
            if section is not None and page not in pages_of.setdefault(section, []):
                pages_of[section].append(page)

    lines = [
        "## Transcripción, agrupada por el esquema del observador",
        "",
        "Cada línea es un segmento: `[<sesión> t=<inicio>-<fin>] texto`. Cítalo con esa sesión y"
        " ese tramo, o con el tramo que cubre varios segmentos seguidos de la misma sesión:"
        " `[^t1]: [Transcripción, <inicio>–<fin>](../sessions/<sesión>/transcript.jsonl"
        "#t=<inicio>-<fin>)`.",
        "",
    ]
    if not log.segments:
        lines.append("(No hay transcripción de este tema.)")

    def walk(parent: str | None, depth: int) -> Iterator[str]:
        for section in state.outline(parent):
            yield f"{'#' * min(depth + 2, 6)} {section.title} (sección {section.id})"
            pages = pages_of.get(section.id)
            if pages:
                yield "Páginas: " + ", ".join(pages)
            concepts = [c.name for c in state.concepts.values() if c.section_id == section.id]
            if concepts:
                yield "Conceptos: " + "; ".join(concepts)
            yield from (segment.line() for segment in by_section.get(section.id, []))
            yield ""
            yield from walk(section.id, depth + 1)

    lines.extend(walk(None, 1))
    unassigned = by_section.get(None, [])
    if unassigned:
        title = "### Sin sección asignada" if state.sections else "### Transcripción completa"
        lines.extend([title, *(segment.line() for segment in unassigned), ""])
    if state.notes:
        lines.extend(["## Observaciones del observador", ""])
        lines.extend(f"- {note.text}" for note in state.notes)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_CLOSED_LABEL = {
    "resolved": "resuelta por el estudiante",
    "auto_resolved": "resuelta por el observador",
    "dismissed": "descartada",
}


def _render_pending(state: TopicState, log: _Log) -> str:
    where: dict[str, _Segment] = {segment.segment_id: segment for segment in log.segments}

    def refs(item: PendingItem) -> str:
        parts = [
            f"{where[s].session_id} t={where[s].span}" for s in item.refs.segments if s in where
        ]
        parts += [log.capture_pages[c] for c in item.refs.pages if c in log.capture_pages]
        parts += list(item.refs.sources)
        return f" ({'; '.join(parts)})" if parts else ""

    lines = ["## Dudas pendientes", ""]
    open_items = state.open_pending()
    closed = state.resolved_pending()
    if not open_items and not closed:
        lines.append("(No hay dudas registradas.)")
    if open_items:
        lines.append("Abiertas (no las resuelvas tú; conserva lo que no esté claro marcado):")
        lines.extend(f"- {item.id} [{item.kind}] {item.text}{refs(item)}" for item in open_items)
        lines.append("")
    if closed:
        lines.append(
            "Cerradas (sigue lo que decidió el estudiante; una duda descartada no es una"
            " decisión sobre el contenido):"
        )
        for item in closed:
            label = _CLOSED_LABEL.get(item.status, item.status)
            outcome = f" -> {item.resolution}" if item.resolution else ""
            lines.append(f"- {item.id} [{item.kind}, {label}] {item.text}{refs(item)}{outcome}")
    return "\n".join(lines).rstrip() + "\n"


# -- sources ---------------------------------------------------------------------------------


def needs_image(transcription: str | None, meta: dict[str, Any] | None = None) -> bool:
    """Whether the editor must see the page image besides its transcription.

    Yes when there is no transcription, when it holds a scheme (a mermaid block, a nested list, an
    arrow) or an uncertain word (`[[?...]]`), or when the sidecar reports a transcription
    confidence below `LOW_CONFIDENCE`.
    """
    if transcription is None or not transcription.strip():
        return True
    confidence = (meta or {}).get("transcription_confidence")
    if isinstance(confidence, int | float) and confidence < LOW_CONFIDENCE:
        return True
    return bool(_UNCERTAIN.search(transcription) or _SCHEME.search(transcription))


def _topic_relative(source: StoredSource, prefix: str) -> str:
    return source.path.removeprefix(prefix + "/")


def _sibling(path: str, suffix: str) -> str:
    stem = path.rsplit(".", 1)[0]
    return f"{stem}.{suffix}"


def _read_text(vault: Vault, vault_path: str) -> str | None:
    try:
        content = read_source(vault, vault_path).content
    except SourceError:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _transcription(vault: Vault, source: StoredSource) -> str | None:
    text = _read_text(vault, _sibling(source.path, "md"))
    if text is None and source.meta is not None:
        stored = source.meta.get("transcription")
        text = stored if isinstance(stored, str) else None
    return text


def _image_block(vault: Vault, source: StoredSource) -> tuple[dict[str, Any], int] | None:
    """The page image (the cropped page when there is one) and its base64 size."""
    for path in (_sibling(source.path, "page.jpg"), source.path):
        try:
            stored = read_source(vault, path)
        except SourceError:
            continue
        if stored.media_type not in IMAGE_MEDIA_TYPES:
            continue
        data = base64.b64encode(stored.content).decode("ascii")
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": stored.media_type, "data": data},
        }
        return block, len(data)
    return None


def _page_number(source_id: str) -> int | None:
    match = _PAGE_NAME.match(source_id.rsplit("/", 1)[-1])
    return int(match["number"]) if match else None


class _Builder:
    """Accumulates the first user message and its record form."""

    def __init__(self, max_images: int, max_bytes: int) -> None:
        self.content: list[dict[str, Any]] = []
        self.record: list[dict[str, Any]] = []
        self.images: list[str] = []
        self.documents: list[str] = []
        self.omitted: list[str] = []
        self.max_images = max_images
        self.budget = max_bytes

    def text(self, text: str) -> None:
        block = {"type": "text", "text": text}
        self.content.append(block)
        self.record.append(dict(block))

    def attachment(self, block: dict[str, Any], size: int, source_id: str, kind: str) -> bool:
        if size > self.budget or (kind == "image" and len(self.images) >= self.max_images):
            self.omitted.append(source_id)
            return False
        self.budget -= size
        (self.images if kind == "image" else self.documents).append(source_id)
        self.content.append(block)
        self.record.append({"type": f"{kind}_ref", "source_id": source_id})
        return True

    def mark_cache(self) -> None:
        """A cache breakpoint on the last block so far (and the record says where it was)."""
        if self.content:
            self.content[-1] = {**self.content[-1], "cache_control": {"type": "ephemeral"}}
            self.record[-1] = {**self.record[-1], "cache_control": {"type": "ephemeral"}}


def _definition(text: str, source_id: str, path: str) -> str:
    """`[texto](../<link>)` for a source; checked against the notes format's own parser."""
    fragment = source_id.partition("#")[2]
    link = f"../{path}" + (f"#{fragment}" if fragment else "")
    definition = f"[{text}]({link})"
    parse_provenance(_Definition(label="x", text=definition))  # raises if the format disagrees
    return definition


def _add_pages(
    vault: Vault,
    builder: _Builder,
    catalogue: list[CitableSource],
    pages: list[tuple[StoredSource, str]],
) -> None:
    for source, source_id in pages:
        number = _page_number(source_id)
        label = PAGE_KIND_TEXT.get(source.kind, source.kind)
        text = f"{label}, página {number}" if number is not None else f"{label}, {source_id}"
        catalogue.append(
            CitableSource(source_id, source.kind, _definition(text, source_id, source_id))
        )
        transcription = _transcription(vault, source)
        header = f"### {text} ({source_id})"
        if transcription is not None and transcription.strip():
            builder.text(f"{header}\nTranscripción de la página:\n\n{transcription.strip()}\n")
        else:
            builder.text(f"{header}\n(Sin transcripción: lee la página en la imagen.)\n")
        if needs_image(transcription, source.meta):
            image = _image_block(vault, source)
            if image is not None:
                builder.attachment(image[0], image[1], source_id, "image")


def _add_pdf(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    builder: _Builder,
    catalogue: list[CitableSource],
    source: StoredSource,
    source_id: str,
) -> None:
    meta = source.meta or {}
    count = _int(meta.get("page_count"))
    name = str(meta.get("original_name") or source_id)
    for page in range(1, count + 1):
        page_id = f"{source_id}#page={page}"
        text = f"PDF, página {original_page(meta, page)}"
        catalogue.append(
            CitableSource(page_id, "pdf", _definition(text, page_id, source_id), f"«{name}»")
        )
    first, last = meta.get("first_page"), meta.get("last_page")
    pages = f", páginas {first}-{last} del original" if first and last else ""
    builder.text(
        f"### PDF «{name}» ({source_id}{pages})\nCita la página K de este documento como"
        f" `{source_id}#page=K`; el catálogo da el número de página del original de cada una.\n"
    )
    block = pdf_document_block(vault, subject_slug, topic_slug, source_id)
    size = len(block["source"]["data"])
    if builder.attachment(block, size, source_id, "document"):
        return
    # Past the budget: the extracted text of each page instead of the document.
    for page in range(1, count + 1):
        text = _read_text(vault, _sibling(source.path, f"p{page:03d}.txt"))
        if text and text.strip():
            builder.text(
                f"#### {source_id}#page={page} (página {original_page(meta, page)} del"
                f" original)\n{text.strip()}\n"
            )


def _add_web(
    vault: Vault,
    builder: _Builder,
    catalogue: list[CitableSource],
    source: StoredSource,
    source_id: str,
) -> None:
    meta = source.meta or {}
    file_name = source_id.rsplit("/", 1)[-1]
    title = str(meta.get("title") or file_name)
    catalogue.append(
        CitableSource(source_id, "web", _definition(f"Web: {title}", source_id, source_id))
    )
    url = meta.get("url")
    text = _read_text(vault, source.path) or ""
    origin = f" -- {url}" if url else ""
    builder.text(f"### Web: {title} ({source_id}{origin})\n\n{text.strip()}\n")


def _topic_block(
    subject_name: str, topic_title: str, mode: FidelityMode, style_guide: str | None
) -> str:
    lines = [
        "# El tema",
        "",
        f"Asignatura: {subject_name}",
        f"Tema: {topic_title}",
        f"Modo de fidelidad: {mode}",
    ]
    if mode == "ampliado":
        lines.append(f"(Contenido ampliado permitido, citado con `[^ia]: {IA_TEXT}`.)")
    else:
        lines.append("(No se permite `[^ia]`: solo lo que está en las fuentes.)")
    lines.append("")
    if style_guide and style_guide.strip():
        lines.extend(["Guía de estilo de la asignatura:", "", style_guide.strip()])
    else:
        lines.append("La asignatura no tiene guía de estilo todavía.")
    return "\n".join(lines) + "\n"


def fidelity_mode_of(value: str) -> FidelityMode:
    """The topic's fidelity mode; anything unknown is the strict default (ADR-0005)."""
    return value if value in FIDELITY_MODES else DEFAULT_FIDELITY  # type: ignore[return-value]


def assemble_input(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    prompt: Prompt,
    digest: DigestReader | None = None,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    instruction: str | None = None,
) -> EditorInput:
    """Everything the editor gets to write the topic's notes (see the module docstring).

    `instruction` replaces the closing request (write the whole `notes/apuntes.md`) for another
    task over the same input, such as the doubts resolution (`doubts.py`).

    Blocking (reads the vault): async callers run it in a worker thread.

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
        ObserverStateError: when the topic's events cannot be folded.
    """
    subject = get_subject(vault, subject_slug).subject
    topic = get_topic(vault, subject_slug, topic_slug).topic
    mode = fidelity_mode_of(topic.fidelity_mode)
    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    builder = _Builder(max_page_images, max_attachment_bytes)
    catalogue: list[CitableSource] = []

    stored = list_sources(vault, subject_slug, topic_slug)
    sources = [(source, _topic_relative(source, prefix)) for source in stored]
    body_start = len(builder.content)
    builder.text("## Fuentes del tema\n")
    for kind in ("notes", "book"):
        pages = [(s, sid) for s, sid in sources if s.kind == kind]
        if pages:
            heading = "Páginas de los apuntes" if kind == "notes" else "Páginas del libro"
            builder.text(f"## {heading}\n")
            _add_pages(vault, builder, catalogue, pages)
    for source, source_id in sources:
        if source.kind == "pdf":
            _add_pdf(vault, subject_slug, topic_slug, builder, catalogue, source, source_id)
    for source, source_id in sources:
        if source.kind == "web":
            _add_web(vault, builder, catalogue, source, source_id)
    if not sources:
        builder.text("(El tema no tiene páginas, PDF ni páginas web guardadas.)\n")
    builder.mark_cache()

    # The catalogue goes first, before the sources it lists (it depends only on them, so it stays
    # inside the cached prefix); transcript spans are cited as the transcript section explains.
    listing = "\n".join(
        f"- `{c.source_id}`{f' {c.description}' if c.description else ''}:"
        f" `[^etiqueta]: {c.definition}`"
        for c in catalogue
    )
    catalogue_text = (
        "## Catálogo de fuentes citables\n\nCopia la definición de nota al pie de la fuente que"
        f" cites:\n\n{listing or '(Ninguna.)'}\n"
    )
    builder.content.insert(body_start, {"type": "text", "text": catalogue_text})
    builder.record.insert(body_start, {"type": "text", "text": catalogue_text})

    state = load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False).state
    log = _read_log(vault, subject_slug, topic_slug)
    builder.text(_render_transcript(state, log))
    builder.text(_render_pending(state, log))
    digest_text = digest(vault, subject_slug, topic_slug) if digest is not None else None
    if digest_text and digest_text.strip():
        builder.text(f"## Resumen del tema (sesiones anteriores)\n\n{digest_text.strip()}\n")
    previous = read_notes(vault, subject_slug, topic_slug)
    if previous and previous.strip():
        builder.text(
            "## Versión actual de los apuntes\n\nConserva las anclas de las secciones que sigan"
            f" existiendo.\n\n{previous}"
        )
    builder.text(
        instruction
        or f"Escribe ahora `notes/apuntes.md` del tema «{topic.title}» en modo de fidelidad"
        f" «{mode}»: el documento Markdown completo y nada más."
    )
    builder.mark_cache()
    return EditorInput(
        subject_slug=subject_slug,
        topic_slug=topic_slug,
        subject_name=subject.name,
        topic_title=topic.title,
        fidelity_mode=mode,
        system=[prompt.content, _topic_block(subject.name, topic.title, mode, subject.style_guide)],
        content=builder.content,
        record_content=builder.record,
        catalogue=catalogue,
        sessions=log.sessions,
        images=builder.images,
        documents=builder.documents,
        omitted=builder.omitted,
        previous_notes=bool(previous and previous.strip()),
        has_digest=bool(digest_text and digest_text.strip()),
    )
