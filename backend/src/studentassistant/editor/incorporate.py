"""Incorporating a few sources into the notes, one small editor request at a time (#326).

The student asks in the chat to incorporate a few pages ("incorpora la página 3", "incorpora las
dos últimas"), and each request is one small, separate `editor` call on the current notes
plus **only** those sources -- never the whole topic at once: the model is slow and worse with large
batches. "Prepárame el tema", when asked, runs the same incorporation over the pending sources in
sequential small batches (`incorporate_pending`).

**States** (`source_status`, a pure reader): each stored source of the topic is `apartada` (set
aside by capture triage, `sources.triage_status`), `incorporada` (the current notes cite it: a
footnote definition links it) or `pendiente`; ordered as the catalogue orders sources (notes
pages, book pages, PDFs, web pages, then pasted images).

**One incorporation** (`incorporate_sources`): the input, stable parts first for caching, is the
`editor_incorporate` prompt and the topic block (system, as `inputs.assemble_input` gives it),
then the current notes (text and block map), the requested sources as `assemble_input` gives them
(a page's transcription, plus its image when `needs_image`; a PDF as its document; a web page as
its text), the transcript segments of each capture's `transcript_window` in the capture's own
session, the facts of each capture requested or cited (`overlap.capture_facts`: uncertain
marks, sharpness, triage flags), the open pending items whose refs name those sources and the
catalogue of just those sources. The editor streams a Spanish reply and calls the strict tool
`apply_edits` (`IncorporationOutput`: the edit ops of `edits.py`, `footnotes`, `summary`,
`nothing_new` and `doubts`), checked like a revision turn -- the ops apply, the notes pass
`validate` with `editor_written=True`, every incorporated source is cited unless `nothing_new`
names it, no settled block («[revisado]») is changed or deleted and no contradiction is raised
between a requested capture and another capture of the same kind not requested now (#474,
`overlap.py`) -- and re-asked at most `MAX_REASKS` times. A source that only repeats what the
notes say goes in `nothing_new`; with no reply of its own, the turn's reply is
`overlap.nothing_new_reply`. It is applied under the per-topic
write lock on the latest notes (re-asked with the new notes when the student saved meanwhile),
committed as `Apuntes de <s>/<t>: incorporada(s) <fuentes>` and recorded as an `incorporation`
conversation record, which the chat shows as a turn of kind `incorporate` and
`revise.undo_last_revision` undoes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.editor.contradictions import detect_contradictions as _detect
from studentassistant.editor.doubts import (
    EditorDoubt,
    LiveSink,
    OpenSessionError,
    editor_doubt_errors,
    raise_doubts,
)
from studentassistant.editor.edits import (
    EditError,
    EditOp,
    NewFootnote,
    apply_edits,
    describe_sections,
)
from studentassistant.editor.generate import (
    DETECTION_FAILED_WARNING,
    OPEN_SESSION_WARNING,
)
from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    STUDENT_LABEL,
    SUPPLEMENTARY_LABEL,
    CitableSource,
    EditorInput,
    _add_pages,
    _add_pdf,
    _add_web,
    _Builder,
    _link_safe,
    _read_log,
    _Segment,
    _topic_block,
    fidelity_mode_of,
)
from studentassistant.editor.notes_format import (
    ProvenanceError,
    notes_revision,
    parse,
    parse_provenance,
    topic_source_resolver,
    validate,
)
from studentassistant.editor.notes_lock import checkpointing, holding_notes
from studentassistant.editor.overlap import (
    capture_facts,
    nothing_new_reply,
    same_kind_contradiction_errors,
    settled_block_errors,
)
from studentassistant.editor.reviewed import open_doubt_sources, settled_blocks
from studentassistant.editor.revise import (
    EDIT_TOOL,
    INCORPORATION_RECORD,
    MAX_REASKS,
    NOT_UNDOABLE_WARNING,
    NOTES_CHANGED_NOTE,
    REPLY_DELTA,
    REPLY_RESTART,
    ChatRequestRef,
    EventSink,
    ReplySink,
    TurnOrigin,
    _changed_sections,
    _Conversation,
    _diff,
    _emit,
    _event_payload,
    _seeded,
    _with_warning,
)
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LLMClient,
    LLMError,
    LLMResponse,
    Prompt,
    RefusalError,
    load_prompt,
    strict_tool,
)
from studentassistant.observer import load_observer_snapshot
from studentassistant.sources import triage_status
from studentassistant.sources.triage import REASON_TEXT
from studentassistant.vault import (
    GitSync,
    StoredSource,
    Vault,
    get_book,
    get_subject,
    get_topic,
    list_sources,
    notes_path,
    read_notes,
    topic_directory,
    write_notes,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "editor_incorporate"
NOTES_INCORPORATED_KIND = "notes.incorporated"
"""The bus event of an applied incorporation (the `IncorporationResult` without `notes`)."""
INCORPORATION_PROGRESS_KIND = "incorporation.progress"
"""`{done, total, source_ids}` after each batch of `incorporate_pending`."""
DEFAULT_MAX_SOURCES = 3
"""`[editor] incorporate_max_sources`: the most sources one request incorporates."""
DEFAULT_BATCH_SIZE = 2
"""`[editor] incorporate_batch_size`: the sources of each batch of `incorporate_pending`."""
INCORPORABLE_KINDS: tuple[str, ...] = ("notes", "book", "pdf", "web")
"""The kinds a request incorporates, in the catalogue's order (a pasted image is already in the
document where the student pasted it)."""
STATUS_ORDER: tuple[str, ...] = (*INCORPORABLE_KINDS, "images")

SourceState = Literal["pendiente", "incorporada", "apartada"]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- errors --------------------------------------------------------------------------------------


class IncorporationError(ValueError):
    """An incorporation that cannot be asked for; the message is Spanish, for the student."""


class NoSourcesError(IncorporationError):
    """The request names no source."""


class TooManySourcesError(IncorporationError):
    """More sources than `[editor] incorporate_max_sources` in one request."""


class UnknownSourceError(IncorporationError):
    """A source the topic does not have, or one that is not incorporated (a pasted image)."""


class SourceSetAsideError(IncorporationError):
    """A source set aside by capture triage (#324): restore it first."""


# -- the states of the sources -----------------------------------------------------------------


class SourceStatus(BaseModel):
    """One stored source of the topic and its state (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="Topic-relative: `sources/notes/page-003.jpg`.")
    kind: str = Field(description="`notes`, `book`, `pdf`, `web` or `images`.")
    number: int | None = Field(
        default=None, description="The number in its file name (`page-003` -> 3), if any."
    )
    label: str = Field(description="How the student names it: «la página 3», «el PDF «tema.pdf»».")
    state: SourceState
    reason: str | None = Field(
        default=None,
        description="`apartada`: why, in Spanish («borrosa», «repetida de la página 1»).",
    )


def _file_number(source_id: str) -> int | None:
    name = source_id.rsplit("/", 1)[-1]
    digits = ""
    for char in name.removeprefix("page-").removeprefix("img-"):
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def _label(kind: str, number: int | None, meta: dict[str, Any] | None) -> str:
    meta = meta or {}
    if kind == "notes":
        return f"la página {number}" if number is not None else "una página de los apuntes"
    if kind == "book":
        return f"la página {number} del libro" if number is not None else "una página del libro"
    if kind == "pdf":
        name = meta.get("original_name")
        return f"el PDF «{_link_safe(name)}»" if isinstance(name, str) and name else "el PDF"
    if kind == "web":
        title = meta.get("title")
        return f"la web «{_link_safe(title)}»" if isinstance(title, str) and title else "la web"
    return f"la imagen pegada {number}" if number is not None else "la imagen pegada"


def _reason(reasons: Sequence[str], duplicate_of: str | None) -> str | None:
    texts: list[str] = []
    for code in reasons:
        text = REASON_TEXT.get(code, code)
        if code in ("duplicate", "same_content") and duplicate_of:
            number = _file_number(duplicate_of)
            if number is not None:
                text = f"{text} de la página {number}"
        texts.append(text)
    return ", ".join(texts) or None


def cited_source_paths(notes: str | None) -> set[str]:
    """The topic-relative files the notes' footnote definitions link (a PDF page's file too)."""
    if not notes:
        return set()
    cited: set[str] = set()
    for definition in parse(notes).footnotes:
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            continue
        if provenance.kind != "transcript" and provenance.path:
            cited.add(provenance.path)
    return cited


def source_status(vault: Vault, subject_slug: str, topic_slug: str) -> list[SourceStatus]:
    """Every stored source of the topic with its state, in catalogue order; reads only (blocking).

    A public function for the chat router (#311, #327) and `GET .../sources/status`.

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
    """
    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    triage = triage_status(vault, subject_slug, topic_slug)
    cited = cited_source_paths(read_notes(vault, subject_slug, topic_slug))
    rows: list[SourceStatus] = []
    for source in _ordered(list_sources(vault, subject_slug, topic_slug)):
        source_id = source.path.removeprefix(prefix + "/")
        number = _file_number(source_id)
        result = triage.get(source_id)
        state: SourceState = "pendiente"
        reason = None
        if result is not None and result.set_aside:
            state, reason = "apartada", _reason(result.reasons, result.duplicate_of)
        elif source_id in cited:
            state = "incorporada"
        rows.append(
            SourceStatus(
                source_id=source_id,
                kind=source.kind,
                number=number,
                label=_label(source.kind, number, source.meta),
                state=state,
                reason=reason,
            )
        )
    return rows


def _ordered(sources: list[StoredSource]) -> list[StoredSource]:
    rank = {kind: index for index, kind in enumerate(STATUS_ORDER)}
    return sorted(sources, key=lambda s: rank.get(s.kind, len(rank)))  # stable within a kind


# -- the tool and the result ---------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncorporationOutput(_Strict):
    """The input of the `apply_edits` tool of an incorporation."""

    ops: list[EditOp] = Field(default_factory=list)
    footnotes: list[NewFootnote] = Field(
        default_factory=list, description="Footnote definitions the new texts need."
    )
    summary: str = Field(description="What this incorporation changes, one short Spanish sentence.")
    nothing_new: list[str] = Field(
        default_factory=list,
        description="The source_ids to incorporate that add nothing new to the notes (said in"
        " the reply); every other one must be cited.",
    )
    doubts: list[EditorDoubt] = Field(
        default_factory=list,
        description="Every point this change leaves unresolved: never written into the notes,"
        " asked in the chat.",
    )


class IncorporationResult(_Strict):
    """What one incorporation did; recorded as the `incorporation` conversation record."""

    subject: str
    topic: str
    turn_id: str | None = None
    origin: TurnOrigin = "typed"
    request: ChatRequestRef | None = None
    source_ids: list[str] = Field(description="The sources incorporated, topic-relative.")
    message: str = Field(description="What was asked, as the chat shows it.")
    reply: str = ""
    applied: bool = False
    summary: str | None = None
    ops: list[EditOp] = Field(default_factory=list)
    footnotes: list[NewFootnote] = Field(default_factory=list)
    nothing_new: list[str] = Field(default_factory=list)
    notes_changed: bool = False
    doubts: list[str] = Field(default_factory=list, description="Pending ids raised.")
    changed_sections: list[str] = Field(default_factory=list)
    diff: str = ""
    notes: str | None = Field(default=None, description="The new notes, when they changed.")
    paths: list[str] = Field(default_factory=list)
    commit: str | None = None
    revision: str | None = None
    attempts: int = 0
    errors: list[str] = Field(default_factory=list)
    warning: str | None = None
    model: str | None = None


# -- the input ---------------------------------------------------------------------------------


INCORPORATE_INSTRUCTION = (
    "## Tarea: incorporar estas fuentes a los apuntes (herramienta `apply_edits`)\n\n"
    "Incorpora a los apuntes actuales lo que aportan las fuentes de arriba, y nada más: el resto"
    " del tema no está aquí. Encaja cada idea en su sitio de la estructura de los apuntes. Las"
    " capturas suelen solaparse (otra foto de la misma página): compara cada una con los apuntes,"
    " no vuelvas a escribir lo que ya dicen (la misma idea, aunque se lea algo distinto) y añade"
    " solo lo nuevo. Sustituye un fragmento solo si la lectura nueva es claramente mejor (antes"
    " había un hueco, una lectura dudosa, un corte o algo ilegible, y la nueva no tiene marca de"
    " duda) y el bloque no está «[revisado]»; una redacción distinta no es mejor. Si una fuente no"
    " aporta nada nuevo, dilo en tu respuesta y ponla en `nothing_new`. Contesta primero con tu"
    " respuesta en texto y llama después una vez a `apply_edits` con todo el cambio.\n"
)


def _notes_block(notes: str, mode: str, settled: Collection[str] = ()) -> str:
    return (
        "## Apuntes actuales\n\nConserva las anclas de las secciones y todo lo que no cambie.\n\n"
        f"{notes.rstrip()}\n\n"
        f"## Mapa de bloques de los apuntes actuales (modo de fidelidad «{mode}»)\n\n"
        f"{describe_sections(notes, settled)}\n"
    )


def _window_segments(segments: list[_Segment], session_id: str, window: Any) -> list[_Segment]:
    if not isinstance(window, dict):
        return []
    start, end = window.get("t_start"), window.get("t_end")
    if not isinstance(start, int) or not isinstance(end, int):
        return []
    return [
        s
        for s in segments
        if s.session_id == session_id and s.end_ms >= start and s.start_ms <= end
    ]


def assemble_incorporation(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    source_ids: Sequence[str],
    notes: str,
    *,
    prompt: Prompt,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> EditorInput:
    """The input of one incorporation (see the module docstring): only `source_ids` (checked
    by the caller: stored, incorporable, not set aside). Blocking."""
    subject = get_subject(vault, subject_slug).subject
    topic = get_topic(vault, subject_slug, topic_slug).topic
    mode = fidelity_mode_of(topic.fidelity_mode)
    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    wanted = set(source_ids)
    sources = [
        (source, source.path.removeprefix(prefix + "/"))
        for source in list_sources(vault, subject_slug, topic_slug)
    ]
    sources = [(source, sid) for source, sid in sources if sid in wanted]
    builder = _Builder(max_page_images, max_attachment_bytes)
    catalogue: list[CitableSource] = []

    builder.text(_notes_block(notes, mode, settled_blocks(vault, subject_slug, topic_slug, notes)))
    builder.text("## Fuentes que incorporar ahora\n")
    book = (
        get_book(vault, subject_slug, topic_slug)
        if any(s.kind == "book" for s, _ in sources)
        else None
    )
    for kind in ("notes", "book"):
        pages = [(s, sid) for s, sid in sources if s.kind == kind]
        if pages:
            heading = "Páginas de los apuntes" if kind == "notes" else "Páginas del libro"
            if kind == "book" and book is not None:
                heading += f" «{_link_safe(book.title)}»"
            label = STUDENT_LABEL if kind == "notes" else SUPPLEMENTARY_LABEL
            builder.text(f"### {heading}\n\n{label}\n")
            _add_pages(vault, builder, catalogue, pages, book.title if book else None)
    for source, source_id in sources:
        if source.kind == "pdf":
            _add_pdf(vault, subject_slug, topic_slug, builder, catalogue, source, source_id)
    for source, source_id in sources:
        if source.kind == "web":
            _add_web(vault, builder, catalogue, source, source_id)

    log = _read_log(vault, subject_slug, topic_slug)
    lines = [
        "## Lo que dijo el estudiante al capturar estas páginas",
        "",
        "Cada línea es un segmento de la transcripción: `[<sesión> t=<inicio>-<fin>] texto`."
        " Cítalo con esa sesión y ese tramo: `[^t1]: [Transcripción, <inicio>–<fin>]"
        "(../sessions/<sesión>/transcript.jsonl#t=<inicio>-<fin>)`.",
        "",
    ]
    spoken = False
    for source, source_id in sources:
        meta = source.meta or {}
        session_id = meta.get("session")
        if not isinstance(session_id, str):
            continue
        segments = _window_segments(log.segments, session_id, meta.get("transcript_window"))
        if segments:
            spoken = True
            lines.append(f"### {source_id}")
            lines.extend(segment.line() for segment in segments)
            lines.append("")
    if not spoken:
        lines.append("(No hay transcripción alrededor de estas páginas.)")
    builder.text("\n".join(lines).rstrip() + "\n")
    facts = capture_facts(vault, subject_slug, topic_slug, source_ids, notes)
    if facts:
        builder.text(facts)

    state = load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False).state
    pending: list[str] = []
    for item in state.open_pending():
        pages = {log.capture_pages.get(capture) for capture in item.refs.pages}
        named = {ref.partition("#")[0] for ref in item.refs.sources} | pages
        if named & wanted:
            pending.append(f"- {item.id} [{item.kind}] {item.text}")
    builder.text(
        "## Dudas abiertas sobre estas fuentes\n\n"
        + (
            "No las resuelvas adivinando: escribe lo que apoyan las fuentes, sin marcas de duda,"
            " o deja fuera lo que no esté claro; ya se le preguntarán al estudiante.\n"
            + "\n".join(pending)
            if pending
            else "(Ninguna.)"
        )
        + "\n"
    )
    listing = "\n".join(
        f"- `{c.source_id}`{f' {c.description}' if c.description else ''}:"
        f" `[^etiqueta]: {c.definition}`"
        for c in catalogue
    )
    builder.text(
        "## Catálogo de las fuentes que incorporar\n\nCopia la definición de nota al pie de la"
        f" fuente que cites.\n\n{listing or '(Ninguna.)'}\n"
    )
    builder.text(INCORPORATE_INSTRUCTION)
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
        previous_notes=bool(notes.strip()),
    )


# -- one incorporation ---------------------------------------------------------------------------


def _checked_ids(
    vault: Vault, subject_slug: str, topic_slug: str, source_ids: Sequence[str], max_sources: int
) -> list[str]:
    """The requested ids, deduplicated and checked; blocking. Raises an `IncorporationError`."""
    ids = list(dict.fromkeys(sid.strip().partition("#")[0] for sid in source_ids if sid.strip()))
    if not ids:
        raise NoSourcesError("Di qué páginas o fuentes quieres incorporar.")
    if len(ids) > max_sources:
        raise TooManySourcesError(
            f"Son demasiadas fuentes para una sola vez: incorpora como mucho {max_sources} de"
            " cada vez, en pasos más pequeños (por ejemplo, «incorpora la página 3»)."
        )
    rows = {row.source_id: row for row in source_status(vault, subject_slug, topic_slug)}
    for source_id in ids:
        row = rows.get(source_id)
        if row is None:
            raise UnknownSourceError(f"El tema no tiene la fuente {source_id}.")
        if row.kind not in INCORPORABLE_KINDS:
            raise UnknownSourceError(
                f"{row.label.capitalize()} ya está en el documento donde la pegaste; no se"
                " incorpora."
            )
        if row.state == "apartada":
            raise SourceSetAsideError(
                f"{row.label.capitalize()} está apartada ({row.reason or 'apartada'});"
                " recupérala antes si quieres incorporarla."
            )
    return ids


def _labels_text(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} y {labels[-1]}"


def _labels(vault: Vault, subject_slug: str, topic_slug: str, ids: list[str]) -> list[str]:
    rows = {row.source_id: row.label for row in source_status(vault, subject_slug, topic_slug)}
    return [rows.get(source_id, source_id) for source_id in ids]


def _parse_call(response: LLMResponse) -> tuple[IncorporationOutput | None, list[str]]:
    calls = [call for call in response.tool_calls if call.name == EDIT_TOOL]
    if not calls:
        return None, [
            f"No has llamado a `{EDIT_TOOL}`: llámala una vez con el cambio (o con `nothing_new`"
            " si las fuentes no aportan nada nuevo)."
        ]
    if response.stop_reason == "max_tokens":
        return None, ["La llamada quedó cortada por el límite de longitud: da un cambio más corto."]
    if len(calls) > 1:
        return None, [f"Llama a `{EDIT_TOOL}` una sola vez, con todo el cambio."]
    try:
        value = IncorporationOutput.model_validate(json.loads(calls[0].input_json))
    except (json.JSONDecodeError, ValidationError) as error:
        return None, [f"La entrada de `{EDIT_TOOL}` no es válida: {error}"]
    return value, []


def _check(
    value: IncorporationOutput,
    notes: str,
    ids: list[str],
    assembled: EditorInput,
    vault: Vault,
    settled: Collection[str] = (),
    doubted: Collection[str] = (),
) -> tuple[list[str], str | None]:
    """`(errors, edited notes)` of an incorporation; `settled` are the keys of the settled
    blocks of `notes`, which it must not change, delete or make cite a new source that an open
    doubt names (`doubted`, or one of the doubts this incorporation raises) (#474)."""
    errors: list[str] = []
    if not value.summary.strip():
        errors.append("Falta el resumen (`summary`) del cambio.")
    for source_id in value.nothing_new:
        if source_id not in ids:
            errors.append(
                f"`nothing_new` solo puede nombrar fuentes que incorporar ahora; {source_id} no"
                " lo es."
            )
    # A contradiction may name a source the notes already cite besides the ones incorporated.
    known = {c.source_id for c in assembled.catalogue}
    extra = [CitableSource(path, "cited", "") for path in sorted(cited_source_paths(notes) - known)]
    errors.extend(
        editor_doubt_errors(
            value.doubts, dataclasses.replace(assembled, catalogue=[*assembled.catalogue, *extra])
        )
    )
    errors.extend(same_kind_contradiction_errors(value.doubts, ids))
    try:
        edited = apply_edits(notes, value.ops, value.footnotes)
    except EditError as error:
        return [*errors, *error.errors], None
    resolver = topic_source_resolver(vault, assembled.subject_slug, assembled.topic_slug)
    errors.extend(
        validate(edited, assembled.fidelity_mode, resolver, editor_written=True, previous=notes)
    )
    raised = {option.source_id for doubt in value.doubts for option in doubt.options} | {
        ref for doubt in value.doubts for ref in doubt.refs
    }
    errors.extend(settled_block_errors(notes, edited, settled, {*doubted, *raised}))
    cited = cited_source_paths(edited)
    for source_id in ids:
        if source_id not in cited and source_id not in value.nothing_new:
            errors.append(
                f"Los apuntes no citan {source_id}: incorpora lo que aporta, citado con su nota"
                " al pie del catálogo, o ponla en `nothing_new` y di en tu respuesta que no"
                " aporta nada nuevo."
            )
    return errors, edited


def _reask_turn(response: LLMResponse, errors: list[str]) -> dict[str, Any]:
    listing = "\n".join(f"- {error}" for error in errors)
    reason = f"El cambio no se puede aplicar por estos motivos:\n\n{listing}"
    content: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": reason}
        for call in response.tool_calls
    ]
    content.append(
        {
            "type": "text",
            "text": f"{reason}\n\nCorrígelos: escribe otra vez una respuesta breve y llama a"
            f" `{EDIT_TOOL}` con el cambio completo corregido.",
        }
    )
    return {"role": "user", "content": content}


def _stale_turn(
    response: LLMResponse, notes: str, mode: str, settled: Collection[str] = ()
) -> dict[str, Any]:
    reason = (
        f"No se ha aplicado el cambio: {NOTES_CHANGED_NOTE}, así que los números de bloque ya no"
        " corresponden."
    )
    content: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": reason}
        for call in response.tool_calls
    ]
    content.append(
        {
            "type": "text",
            "text": f"{reason}\n\n{_notes_block(notes, mode, settled)}\nRespeta lo que ha"
            " escrito el estudiante: escribe otra vez una respuesta breve y llama a"
            f" `{EDIT_TOOL}` con la incorporación completa sobre estos apuntes.",
        }
    )
    return {"role": "user", "content": content}


def _apply_if_current(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    *,
    base: str | None,
    edited: str,
    message: str,
) -> tuple[tuple[list[str], str | None] | None, str | None]:
    """Write and commit under the write lock when the notes are still `base`; blocking.

    `(applied, current)`: `applied` is `(paths, commit)`, or `None` when the stored notes are
    no longer `base` (nothing written); `current` is the stored notes read under the lock.
    """
    with holding_notes(vault, subject_slug, topic_slug):
        current = read_notes(vault, subject_slug, topic_slug)
        if current != base:
            return None, current
        if edited == (current or ""):
            return ([], None), current
        # One locked step (`checkpointing`): the sync loop cannot commit the notes before the
        # incorporation's own commit, which is the one an undo reverts.
        with checkpointing(sync) as commit_now:
            write_notes(vault, subject_slug, topic_slug, edited)
            path = notes_path(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
            return ([path], commit_now(message)), current


async def incorporate_sources(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    source_ids: Sequence[str],
    *,
    client: LLMClient,
    sync: GitSync,
    on_reply: ReplySink | None = None,
    on_event: EventSink | None = None,
    request: ChatRequestRef | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    turn_id: str | None = None,
    max_sources: int = DEFAULT_MAX_SOURCES,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    live: LiveSink | None = None,
    host: str | None = None,
) -> IncorporationResult:
    """Incorporate a few sources into the notes with one editor request (module docstring).

    `source_ids` are topic-relative (`sources/notes/page-003.jpg`; a PDF page's `#page=K` is
    dropped: the PDF is the source), at most `max_sources`. `request` is the spoken request the
    incorporation answers (origin `voice`); `turn_id` is stored as given. The reply streams to
    `on_reply` (`reply.delta`, `reply.restart`); an applied change goes to `on_event` as
    `notes.incorporated`. Its doubts are raised as a revision turn's are (`live`, `host`).

    Raises:
        NoSourcesError, TooManySourcesError, UnknownSourceError, SourceSetAsideError: before any
            call; nothing written.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing
            written but the conversation records of the calls made.
    """
    ids = await asyncio.to_thread(
        _checked_ids, vault, subject_slug, topic_slug, source_ids, max_sources
    )
    labels = await asyncio.to_thread(_labels, vault, subject_slug, topic_slug, ids)
    what = _labels_text(labels)
    message = request.text if request is not None else f"Incorpora {what}."
    base = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    title = (await asyncio.to_thread(get_topic, vault, subject_slug, topic_slug)).topic.title
    notes = _seeded(base, title)
    prompt = load_prompt(PROMPT_NAME)
    assembled = await asyncio.to_thread(
        partial(
            assemble_incorporation,
            vault,
            subject_slug,
            topic_slug,
            ids,
            notes,
            prompt=prompt,
            max_page_images=max_page_images,
            max_attachment_bytes=max_attachment_bytes,
        )
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": assembled.content}]
    tool = strict_tool(
        EDIT_TOOL,
        "Apply this incorporation to the notes: edit ops, new footnotes, a summary, the sources"
        " that add nothing new and the doubts left.",
        IncorporationOutput,
    )
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)
    verb = "incorporada" if len(ids) == 1 else "incorporadas"
    commit_message = f"Apuntes de {subject_slug}/{topic_slug}: {verb} {what}"

    model = client.model
    value: IncorporationOutput | None = None
    edited: str | None = None
    applied: tuple[list[str], str | None] | None = None
    stale = False
    errors: list[str] = []
    reply = ""
    attempts = 0
    for attempt in range(1, MAX_REASKS + 2):
        attempts = attempt
        if attempt > 1 and on_reply is not None:
            await on_reply(REPLY_RESTART, {"attempt": attempt})

        async def on_text(delta: str, attempt: int = attempt) -> None:
            if on_reply is not None:
                await on_reply(REPLY_DELTA, {"text": delta, "attempt": attempt})

        response = await client.create(
            messages,
            system=assembled.system,
            tools=[tool],
            tool_choice={"type": "auto"},
            prompt_hash=prompt.hash,
            confirm_over_cap=confirm_over_cap,
            on_text=on_text if on_reply is not None else None,
        )
        model = response.model or model
        if attempt == 1:
            await conversation.record(
                "context",
                model=client.model,
                prompt_hash=prompt.hash,
                detail={"reason": "incorporate", "incorporated": ids, **assembled.summary()},
            )
            await conversation.record(
                "user",
                message={"role": "user", "content": assembled.record_content},
                model=client.model,
            )
        await conversation.record(
            "assistant",
            message=response.assistant_turn(),
            model=model,
            prompt_hash=prompt.hash,
            usage=response.usage.model_dump(),
        )
        if response.stop_reason == "refusal":
            raise RefusalError("the editor declined to incorporate the sources")
        reply = response.text.strip()
        value, errors = _parse_call(response)
        stale = False
        edited = None
        if value is not None:
            # Both from the notes before this turn's edits: the turn cannot unlock what it edits.
            settled = await asyncio.to_thread(
                settled_blocks, vault, subject_slug, topic_slug, notes
            )
            doubted = await asyncio.to_thread(open_doubt_sources, vault, subject_slug, topic_slug)
            errors, edited = await asyncio.to_thread(
                _check, value, notes, ids, assembled, vault, settled, doubted
            )
        if value is not None and edited is not None and not errors:
            applied, current = await asyncio.to_thread(
                partial(
                    _apply_if_current,
                    vault,
                    sync,
                    subject_slug,
                    topic_slug,
                    base=base,
                    edited=edited,
                    message=commit_message,
                )
            )
            if applied is None:
                stale = True
                base, notes = current, _seeded(current, title)
                errors = [f"No se ha aplicado: {NOTES_CHANGED_NOTE}."]
        await conversation.record("validation", detail={"attempt": attempt, "errors": errors})
        if not errors or attempt > MAX_REASKS:
            break
        reask = (
            _stale_turn(
                response,
                notes,
                assembled.fidelity_mode,
                await asyncio.to_thread(settled_blocks, vault, subject_slug, topic_slug, notes),
            )
            if stale
            else _reask_turn(response, errors)
        )
        messages = [*messages, response.assistant_turn(), reask]
        await conversation.record("user", message=reask, model=model)

    result = IncorporationResult(
        subject=subject_slug,
        topic=topic_slug,
        turn_id=turn_id,
        origin="typed" if request is None else "voice",
        request=request,
        source_ids=ids,
        message=message,
        reply=reply,
        attempts=attempts,
        model=model,
    )
    if errors:
        warning = (
            "El editor no ha podido incorporar las fuentes porque has cambiado los apuntes"
            " mientras respondía; los apuntes se quedan como los dejaste. Vuelve a pedírselo."
            if stale
            else "El editor no ha podido incorporar las fuentes cumpliendo las reglas de"
            " procedencia; los apuntes no han cambiado. Prueba con menos páginas."
        )
        result = result.model_copy(update={"errors": errors, "warning": warning})
    elif value is not None and edited is not None and applied is not None:
        paths, commit = applied
        changed = bool(paths)
        result = result.model_copy(
            update={
                "reply": reply
                or (
                    nothing_new_reply(value.nothing_new)
                    if value.nothing_new and not changed
                    else value.summary.strip()
                ),
                "applied": changed,
                "summary": value.summary.strip() or None,
                "ops": value.ops,
                "footnotes": value.footnotes,
                "nothing_new": value.nothing_new,
                "notes_changed": changed,
                "changed_sections": _changed_sections(value.ops) if changed else [],
                "diff": _diff(notes, edited) if changed else "",
                "notes": edited if changed else None,
                "paths": paths,
                "commit": commit,
                "warning": NOT_UNDOABLE_WARNING if paths and commit is None else None,
            }
        )
    if value is not None and not errors and value.doubts:
        try:
            raised = await raise_doubts(
                vault, subject_slug, topic_slug, value.doubts, sync=sync, host=host, live=live
            )
        except Exception:
            logger.exception(
                "could not record the doubts of an incorporation of %s/%s", subject_slug, topic_slug
            )
            result = result.model_copy(
                update={
                    "warning": _with_warning(
                        result.warning,
                        "Las fuentes están incorporadas, pero no se han podido guardar las"
                        " dudas que ha encontrado el editor.",
                    )
                }
            )
        else:
            result = result.model_copy(update={"doubts": raised})
    if result.notes_changed and result.notes is not None:
        revision: str | None = notes_revision(result.notes)
    else:
        stored = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
        revision = None if stored is None else notes_revision(stored)
    result = result.model_copy(update={"revision": revision})
    payload = result.model_dump(mode="json")
    await conversation.record(
        INCORPORATION_RECORD, model=model, prompt_hash=prompt.hash, detail=payload
    )
    sync.note_change()
    if result.applied:
        await _emit(
            on_event, NOTES_INCORPORATED_KIND, _event_payload(payload), subject_slug, topic_slug
        )
    return result


# -- the whole topic in small batches ------------------------------------------------------------


class BatchTurns(Protocol):
    """How a caller follows each batch of `incorporate_pending` as a chat turn of its own."""

    def begin(self, source_ids: list[str]) -> tuple[str | None, ReplySink | None]:
        """A batch starts: its turn id and the sink its reply streams to."""
        ...

    def end(self, result: IncorporationResult | None, error: BaseException | None) -> None:
        """The batch ended with `result`, or failed with `error`."""
        ...


ProgressSink = Callable[[dict[str, Any]], Any]
"""Receives `{done, total, source_ids}` after each batch (may be sync or async)."""


class PendingIncorporationResult(_Strict):
    """What `incorporate_pending` did over the topic's pending sources."""

    subject: str
    topic: str
    total: int = Field(description="The pending sources when it started.")
    done: list[str] = Field(default_factory=list, description="Sources processed, in order.")
    remaining: list[str] = Field(
        default_factory=list, description="Pending sources not reached (a stop)."
    )
    batches: list[IncorporationResult] = Field(default_factory=list)
    stopped: bool = Field(default=False, description="A batch failed: calling again continues.")
    notes_changed: bool = False
    version: int | None = Field(default=None, description="`N` of the new `apuntes-vN` tag.")
    tag: str | None = None
    commit: str | None = None
    contradictions: list[str] = Field(default_factory=list)
    doubts: list[str] = Field(default_factory=list)
    revision: str | None = None
    attempts: int = 0
    warning: str | None = None
    model: str | None = None


NOTHING_PENDING_WARNING = "No hay fuentes pendientes de incorporar en este tema."


def pending_batches(
    vault: Vault, subject_slug: str, topic_slug: str, batch_size: int
) -> list[list[str]]:
    """The topic's `pendiente` incorporable sources in catalogue order (notes pages first, then
    book, PDF, web), cut in batches of `batch_size`; blocking."""
    size = max(1, batch_size)
    ids = [
        row.source_id
        for row in source_status(vault, subject_slug, topic_slug)
        if row.state == "pendiente" and row.kind in INCORPORABLE_KINDS
    ]
    return [ids[index : index + size] for index in range(0, len(ids), size)]


async def _progress(on_progress: ProgressSink | None, payload: dict[str, Any]) -> None:
    if on_progress is None:
        return
    try:
        outcome = on_progress(payload)
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception:
        logger.exception("could not publish the incorporation progress")


def _stop_warning(error: BaseException, done: int, total: int) -> str:
    kept = f"Se han incorporado {done} de {total} fuentes y se quedan en los apuntes. "
    if isinstance(error, CostConfirmationRequiredError):
        return kept + "Se ha alcanzado el límite de gasto: confírmalo para seguir con el resto."
    if isinstance(error, RefusalError):
        return kept + "Claude se ha negado a incorporar las siguientes; vuelve a pedirlo."
    if isinstance(error, IncorporationError):
        return kept + str(error)
    return kept + "Claude no ha respondido; vuelve a pedirlo para seguir con el resto."


async def incorporate_pending(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    client: LLMClient,
    sync: GitSync,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_sources: int = DEFAULT_MAX_SOURCES,
    on_event: EventSink | None = None,
    on_progress: ProgressSink | None = None,
    turns: BatchTurns | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    detect_contradictions: bool = True,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    live: LiveSink | None = None,
    host: str | None = None,
) -> PendingIncorporationResult:
    """ "Prepárame el tema" in small batches: `incorporate_sources` over the pending sources.

    One batch after another (`pending_batches`, at most `max_sources` each), each its own
    request, commit and chat turn (`turns`), with `incorporation.progress` to `on_progress`
    after each. A cost cap, a refusal, a failed call or a batch that could not be applied stops
    it: the batches done are kept and the result is `stopped` with a Spanish `warning` (calling
    again continues with what is still pending) -- except a failure of the very first batch,
    which is raised as `incorporate_sources` raises it. At the end, when the notes changed, the
    topic's next notes version is tagged (`GitSync.create_notes_tag`) and the contradictions
    between its sources are searched (`contradictions.detect_contradictions`, as a generation
    does; a failure there is only a warning).
    """
    batches = await asyncio.to_thread(
        pending_batches, vault, subject_slug, topic_slug, min(batch_size, max_sources)
    )
    total = sum(len(batch) for batch in batches)
    result = PendingIncorporationResult(
        subject=subject_slug, topic=topic_slug, total=total, model=client.model
    )
    if not batches:
        stored = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
        return result.model_copy(
            update={
                "warning": NOTHING_PENDING_WARNING,
                "revision": None if stored is None else notes_revision(stored),
            }
        )
    done: list[str] = []
    results: list[IncorporationResult] = []
    warning: str | None = None
    stopped = False
    for index, batch in enumerate(batches):
        turn_id, on_reply = turns.begin(batch) if turns is not None else (None, None)
        try:
            outcome = await incorporate_sources(
                vault,
                subject_slug,
                topic_slug,
                batch,
                client=client,
                sync=sync,
                on_reply=on_reply,
                on_event=on_event,
                confirm_over_cap=confirm_over_cap,
                clock=clock,
                turn_id=turn_id,
                max_sources=max_sources,
                max_page_images=max_page_images,
                max_attachment_bytes=max_attachment_bytes,
                live=live,
                host=host,
            )
        except (IncorporationError, LLMError) as error:
            if turns is not None:
                turns.end(None, error)
            if not results:
                raise
            stopped, warning = True, _stop_warning(error, len(done), total)
            remaining = [sid for later in batches[index:] for sid in later]
            break
        if turns is not None:
            turns.end(outcome, None)
        results.append(outcome)
        if outcome.errors:
            stopped = True
            warning = (
                f"Se han incorporado {len(done)} de {total} fuentes. {outcome.warning or ''}"
            ).strip()
            remaining = [sid for later in batches[index:] for sid in later]
            break
        done.extend(batch)
        await _progress(
            on_progress,
            {"done": len(done), "total": total, "source_ids": batch},
        )
    else:
        remaining = []

    changed = any(outcome.notes_changed for outcome in results)
    result = result.model_copy(
        update={
            "done": done,
            "remaining": remaining,
            "batches": results,
            "stopped": stopped,
            "notes_changed": changed,
            "doubts": [pid for outcome in results for pid in outcome.doubts],
            "attempts": sum(outcome.attempts for outcome in results),
            "model": next((o.model for o in reversed(results) if o.model), client.model),
            "warning": warning,
        }
    )
    if changed:
        title = (await asyncio.to_thread(get_topic, vault, subject_slug, topic_slug)).topic.title
        tag = await asyncio.to_thread(_tag, sync, subject_slug, topic_slug, title)
        result = result.model_copy(
            update={"version": tag.version, "tag": tag.name, "commit": tag.commit}
        )
        citable = await asyncio.to_thread(source_status, vault, subject_slug, topic_slug)
        if detect_contradictions and sum(row.state != "apartada" for row in citable) >= 2:
            result = await _with_contradictions(
                result,
                vault,
                subject_slug,
                topic_slug,
                client=client,
                sync=sync,
                host=host,
                confirm_over_cap=confirm_over_cap,
                clock=clock,
                max_page_images=max_page_images,
                max_attachment_bytes=max_attachment_bytes,
            )
    stored = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    return result.model_copy(
        update={"revision": None if stored is None else notes_revision(stored)}
    )


def _tag(sync: GitSync, subject_slug: str, topic_slug: str, title: str) -> Any:
    existing = sync.list_notes_tags(subject_slug, topic_slug)
    version = (existing[-1].version if existing else 0) + 1
    return sync.create_notes_tag(
        subject_slug, topic_slug, f"Apuntes v{version} de {subject_slug}/{topic_slug}: {title}"
    )


async def _with_contradictions(
    result: PendingIncorporationResult,
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    **options: Any,
) -> PendingIncorporationResult:
    def joined(extra: str | None) -> str | None:
        return " ".join(part for part in (result.warning, extra) if part) or None

    try:
        found = await _detect(vault, subject_slug, topic_slug, **options)
    except OpenSessionError:
        return result.model_copy(update={"warning": joined(OPEN_SESSION_WARNING)})
    except Exception:
        logger.exception("contradiction detection failed for %s/%s", subject_slug, topic_slug)
        return result.model_copy(update={"warning": joined(DETECTION_FAILED_WARNING)})
    return result.model_copy(
        update={"contradictions": found.raised, "warning": joined(found.warning)}
    )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_SOURCES",
    "INCORPORABLE_KINDS",
    "INCORPORATION_PROGRESS_KIND",
    "NOTES_INCORPORATED_KIND",
    "NOTHING_PENDING_WARNING",
    "BatchTurns",
    "IncorporationError",
    "IncorporationOutput",
    "IncorporationResult",
    "NoSourcesError",
    "PendingIncorporationResult",
    "SourceSetAsideError",
    "SourceStatus",
    "TooManySourcesError",
    "UnknownSourceError",
    "assemble_incorporation",
    "cited_source_paths",
    "incorporate_pending",
    "incorporate_sources",
    "pending_batches",
    "source_status",
]
