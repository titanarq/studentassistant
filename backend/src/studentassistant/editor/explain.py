""" "¿Por qué pusiste esto?": the editor explains one block of the notes from its cited sources.

The student points at a block of `notes/apuntes.md` -- its section anchor and block number, as the
validator numbers them, plus optionally the text they see (`quote`), which finds the block again
when the numbers of the web view and of the parser differ or the notes changed meanwhile. The
editor (`editor` role, prompt `editor_explain`) is given only what that block rests on, looked at
again:

- the block, its section (as context) and its footnote definitions;
- every source it cites: a notes or book page as its transcription **and its page image** (always,
  the cropped page when there is one), a PDF page as its extracted text (the stored PDF itself when
  there is none), a web snapshot as its text, a transcript span as the segments of that session
  around it (the ones inside the span marked), `[^ia]` as what it is -- added by the AI;
- the decisions on the topic's doubts (the observer's closed pending items), since they explain
  choices ("pone 1791, lo dijiste tú").

It answers in Spanish, plain text, streamed as it is written (`on_reply("reply.delta", ...)`, as in
`revise.py`). Nothing of the notes changes. The answer is appended to the editor conversation
(`conversations/editor.jsonl`: `context` with reason `explain`, `user`, `assistant`, then one
`explanation` record, the `ExplanationResult`), so the chat history shows it as a turn and the
revision conversation that follows sees it. The result carries the block's `refs`: one per cited
footnote, with its label, for the web's sources panel.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    _Builder,
    _image_block,
    _read_log,
    _read_text,
    _render_pending,
    _Segment,
    _topic_block,
    _transcription,
    fidelity_mode_of,
)
from studentassistant.editor.notes_format import (
    Block,
    NotesDocument,
    Provenance,
    ProvenanceError,
    parse,
    parse_provenance,
)
from studentassistant.editor.revise import (
    EXPLANATION_RECORD,
    ChatRef,
    InvalidMessageError,
    NotesMissingError,
    ReplySink,
    _Conversation,
)
from studentassistant.llm import LLMClient, RefusalError, load_prompt
from studentassistant.observer import load_observer_snapshot
from studentassistant.sources import original_page, pdf_document_block, read_pdf_page_text
from studentassistant.vault import (
    GitSync,
    StoredSource,
    Vault,
    get_subject,
    get_topic,
    list_sources,
    read_notes,
    topic_directory,
)

PROMPT_NAME = "editor_explain"
REPLY_DELTA = "reply.delta"
MAX_QUOTE_CHARS = 2000
MAX_WEB_CHARS = 30_000
TRANSCRIPT_CONTEXT_MS = 30_000
"""How much transcript around a cited span the editor also reads, each side."""
QUESTION_EXCERPT_CHARS = 280

Clock = Callable[[], datetime]

_REFERENCE = re.compile(r"\[\^[^\]\s]+\](?!:)")
_UNCERTAIN = re.compile(r"\[\[\?([^\]]*)\]\]")
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+", re.MULTILINE)
_SPAN = re.compile(
    r"^sessions/(?P<session>[^/#]+)#t=(?P<start>\d{2}:\d{2}:\d{2})-(?P<end>\d{2}:\d{2}:\d{2})$"
)
_CONTENT_KINDS = frozenset({"paragraph", "list", "table"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BlockNotFoundError(InvalidMessageError):
    """The block pointed at is not in the current notes (or it is not one that cites sources)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockAnchor(_Strict):
    """Which block to explain: `section` (anchor without `#`, `None` for the preamble), `block`
    (1-based, as the validator numbers them) and/or `quote` (the block's text as the student
    sees it, or its beginning: it finds the block when the number does not match)."""

    section: str | None = None
    block: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=MAX_QUOTE_CHARS)


class ExplanationResult(_Strict):
    """One "¿Por qué pusiste esto?" answer; recorded as the `explanation` conversation record."""

    subject: str
    topic: str
    section: str | None = Field(description="The anchor of the block's section; None: preamble.")
    block: int = Field(description="The block's number in its section, from 1.")
    block_text: str = Field(description="The block as written in `apuntes.md`.")
    question: str = Field(description="The question as the chat shows it, Spanish.")
    reply: str = Field(description="The editor's explanation, Spanish.")
    refs: list[ChatRef] = Field(default_factory=list, description="The block's cited sources.")
    images: list[str] = Field(default_factory=list, description="Pages whose image was sent.")
    omitted: list[str] = Field(default_factory=list, description="Sources left out (budget).")
    warning: str | None = None
    model: str | None = None


# -- finding the block -------------------------------------------------------------------------


def _plain(text: str) -> str:
    """Only letters and digits, casefolded: how a block and a quote of it are compared."""
    text = _UNCERTAIN.sub(r"\1", _REFERENCE.sub("", text))
    text = _LIST_MARKER.sub("", text)
    return "".join(character for character in text.casefold() if character.isalnum())


def _matches(block: Block, quote: str) -> bool:
    wanted = _plain(quote.rstrip().removesuffix("…"))
    return bool(wanted) and wanted in _plain(block.text)


def find_block(document: NotesDocument, anchor: BlockAnchor) -> tuple[str | None, int, Block]:
    """`(section anchor, number, block)` of the block `anchor` points at.

    The numbered block is taken when it exists and, with a `quote`, matches it; otherwise the first
    block of the section (then of the whole notes) that holds the quote.

    Raises:
        BlockNotFoundError: nothing matches, or the block is not a paragraph, list or table.
    """
    sections: list[tuple[str | None, list[Block]]] = [(None, document.preamble)]
    sections += [(section.anchor, section.blocks) for section in document.sections]
    home = [(name, blocks) for name, blocks in sections if name == anchor.section]
    found: tuple[str | None, int, Block] | None = None
    if home and anchor.block is not None and anchor.block <= len(home[0][1]):
        block = home[0][1][anchor.block - 1]
        if anchor.quote is None or _matches(block, anchor.quote):
            found = (home[0][0], anchor.block, block)
    if found is None and anchor.quote is not None:
        for name, blocks in [*home, *(s for s in sections if s not in home)]:
            for number, block in enumerate(blocks, start=1):
                if block.kind in _CONTENT_KINDS and _matches(block, anchor.quote):
                    found = (name, number, block)
                    break
            if found is not None:
                break
    if found is None:
        raise BlockNotFoundError(
            "No encuentro ese fragmento en los apuntes actuales: vuelve a cargarlos y pregunta otra"
            " vez."
        )
    if found[2].kind not in _CONTENT_KINDS:
        raise BlockNotFoundError("Solo puedo explicar un párrafo, una lista o una tabla.")
    return found


def _excerpt(block: Block, width: int = QUESTION_EXCERPT_CHARS) -> str:
    words = " ".join(_UNCERTAIN.sub(r"\1", _REFERENCE.sub("", block.text)).split())
    return words if len(words) <= width else words[:width].rstrip() + "…"


def _question(section: str | None, block: Block) -> str:
    where = f" (en la sección #{section})" if section is not None else ""
    return f"¿Por qué pusiste esto?{where} «{_excerpt(block)}»"


# -- the refs ----------------------------------------------------------------------------------


def _refs(document: NotesDocument, block: Block) -> tuple[list[ChatRef], list[str]]:
    """The block's cited footnotes as refs, and the labels that cite nothing usable."""
    definitions = {d.label: d for d in reversed(document.footnotes)}  # the first one wins
    refs: list[ChatRef] = []
    broken: list[str] = []
    for label in block.footnote_refs:
        definition = definitions.get(label)
        if definition is None:
            broken.append(label)
            continue
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            broken.append(label)
            continue
        refs.append(_ref(label, provenance))
    return refs, broken


def _ref(label: str, provenance: Provenance) -> ChatRef:
    return ChatRef(
        label=label,
        kind=provenance.kind,
        text=provenance.text,
        source_id=provenance.source_id,
        path=provenance.path,
    )


# -- the input ---------------------------------------------------------------------------------


def _seconds(clock: str) -> int:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _add_page(vault: Vault, builder: _Builder, ref: ChatRef, source: StoredSource) -> None:
    transcription = _transcription(vault, source)
    header = f"### [^{ref.label}] {ref.text} ({ref.source_id})"
    if transcription is not None and transcription.strip():
        builder.text(f"{header}\nTranscripción de la página:\n\n{transcription.strip()}\n")
    else:
        builder.text(f"{header}\n(Sin transcripción: lee la página en la imagen.)\n")
    image = _image_block(vault, source)
    if image is not None:
        builder.attachment(image[0], image[1], ref.source_id or source.path, "image")


def _add_pdf_page(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    builder: _Builder,
    ref: ChatRef,
    source: StoredSource,
    stored_id: str,
) -> None:
    meta = source.meta or {}
    page_text = (ref.source_id or "").partition("#page=")[2]
    page = int(page_text) if page_text.isdigit() else None
    name = str(meta.get("original_name") or stored_id)
    where = f", página {original_page(meta, page)} del original" if page is not None else ""
    header = f"### [^{ref.label}] {ref.text} (PDF «{name}»{where})"
    page_text = read_pdf_page_text(vault, source.path, page) if page else None
    if page_text is not None:
        label = (
            "Transcripción de la página escaneada"
            if page_text.transcribed
            else "Texto de la página"
        )
        builder.text(f"{header}\n{label}:\n\n{page_text.text}\n")
        return
    builder.text(f"{header}\n(Sin texto extraído: lee la página en el documento.)\n")
    block = pdf_document_block(vault, subject_slug, topic_slug, stored_id)
    builder.attachment(block, len(block["source"]["data"]), stored_id, "document")


def _add_web(vault: Vault, builder: _Builder, ref: ChatRef, source: StoredSource) -> None:
    meta = source.meta or {}
    url = meta.get("url")
    text = (_read_text(vault, source.path) or "").strip()
    if len(text) > MAX_WEB_CHARS:
        text = text[:MAX_WEB_CHARS].rstrip() + "\n[…]"
    origin = f" -- {url}" if url else ""
    builder.text(f"### [^{ref.label}] {ref.text} ({ref.source_id}{origin})\n\n{text}\n")


def _add_span(builder: _Builder, ref: ChatRef, segments: list[_Segment]) -> None:
    header = f"### [^{ref.label}] {ref.text} ({ref.source_id})"
    match = _SPAN.match(ref.source_id or "")
    if match is None:
        builder.text(f"{header}\n(No se entiende el tramo citado.)\n")
        return
    start, end = _seconds(match["start"]) * 1000, _seconds(match["end"]) * 1000
    lines: list[str] = []
    for segment in segments:
        if segment.session_id != match["session"]:
            continue
        if segment.end_ms < start - TRANSCRIPT_CONTEXT_MS:
            continue
        if segment.start_ms > end + TRANSCRIPT_CONTEXT_MS:
            continue
        inside = segment.end_ms > start and segment.start_ms < end
        lines.append(f"{segment.line()}{'  <- citado' if inside else ''}")
    body = "\n".join(lines) if lines else "(No hay transcripción en ese tramo.)"
    builder.text(
        f"{header}\nLo que dijo el estudiante (reconocimiento de voz; las líneas marcadas son el"
        f" tramo citado, las demás lo rodean):\n\n{body}\n"
    )


def _instruction(question: str) -> str:
    return (
        "## Tarea: «¿Por qué pusiste esto?»\n\n"
        f"El estudiante pregunta: {question}\n\n"
        "Explícale en español, breve y como un tutor, por qué está este bloque en sus apuntes y de"
        " dónde sale: qué dice cada fuente citada (la página, lo que dijo, el libro...), citándola"
        " por su nombre, y si algo del bloque no está en ellas o lo añadiste tú, dilo. No cambies"
        " los apuntes: si ves un error, díselo y propón que te pida el cambio."
    )


def assemble_explanation(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    document: NotesDocument,
    section: str | None,
    number: int,
    block: Block,
    refs: list[ChatRef],
    broken: list[str],
    question: str,
    *,
    system_prompt: str,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> tuple[list[str], _Builder]:
    """The system blocks and the message for one explanation; blocking (reads the vault)."""
    subject = get_subject(vault, subject_slug).subject
    topic = get_topic(vault, subject_slug, topic_slug).topic
    mode = fidelity_mode_of(topic.fidelity_mode)
    system = [system_prompt, _topic_block(subject.name, topic.title, mode, subject.style_guide)]
    builder = _Builder(max_page_images, max_attachment_bytes)

    if section is not None:
        home = document.section(section)
        context = home.render().rstrip()
        where = f"la sección #{section} («{home.heading.title}»)"
    else:
        context = "".join(b.render() for b in document.preamble).rstrip()
        where = "la introducción, antes de la primera sección"
    definitions = {d.label: d for d in reversed(document.footnotes)}
    cited = [
        f"[^{label}]: {definitions[label].text}"
        for label in block.footnote_refs
        if label in definitions
    ]
    builder.text(
        f"## El bloque\n\nBloque {number} de {where}:\n\n{block.text}\n\n"
        f"Sus notas al pie:\n\n{chr(10).join(cited) or '(Ninguna.)'}\n"
    )
    if broken:
        builder.text(
            "Estas notas no citan una fuente del tema (sin definir o mal escritas): "
            + ", ".join(f"[^{label}]" for label in broken)
            + "\n"
        )
    builder.text(f"## La sección entera, como contexto\n\n{context}\n")

    prefix = topic_directory(vault, subject_slug, topic_slug).relative_to(vault.path).as_posix()
    stored = {
        source.path.removeprefix(prefix + "/"): source
        for source in list_sources(vault, subject_slug, topic_slug)
    }
    log = _read_log(vault, subject_slug, topic_slug)
    builder.text("## Las fuentes que cita el bloque\n")
    for ref in refs:
        if ref.kind == "ia":
            builder.text(
                f"### [^{ref.label}] {ref.text}\nEsta parte no sale de las fuentes: la añadiste"
                " tú (modo ampliado).\n"
            )
            continue
        if ref.kind == "transcript":
            _add_span(builder, ref, log.segments)
            continue
        source = stored.get(ref.path or "")
        if source is None:
            builder.text(
                f"### [^{ref.label}] {ref.text} ({ref.source_id})\n(Esta fuente ya no está en"
                " el tema.)\n"
            )
        elif ref.kind in ("notes", "book"):
            _add_page(vault, builder, ref, source)
        elif ref.kind == "pdf":
            _add_pdf_page(vault, subject_slug, topic_slug, builder, ref, source, ref.path or "")
        else:
            _add_web(vault, builder, ref, source)
    if not refs:
        builder.text("(El bloque no cita ninguna fuente del tema.)\n")

    state = load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False).state
    builder.text(_render_pending(state, log))
    builder.text(_instruction(question))
    return system, builder


# -- the call ----------------------------------------------------------------------------------


async def explain_block(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    anchor: BlockAnchor,
    *,
    client: LLMClient,
    sync: GitSync | None = None,
    on_reply: ReplySink | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> ExplanationResult:
    """Explain one block of the notes from its cited sources (see the module docstring).

    `sync`, when given, is told the conversation changed (the sync loop commits it).

    Raises:
        NotesMissingError: the topic has no notes yet; nothing sent.
        BlockNotFoundError: the anchor points at no paragraph, list or table; nothing sent.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing
            written but the conversation records of the call.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    notes = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    if not notes or not notes.strip():
        raise NotesMissingError(
            "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."
        )
    document = parse(notes)
    section, number, block = find_block(document, anchor)
    refs, broken = _refs(document, block)
    question = _question(section, block)
    prompt = load_prompt(PROMPT_NAME)
    system, builder = await asyncio.to_thread(
        assemble_explanation,
        vault,
        subject_slug,
        topic_slug,
        document,
        section,
        number,
        block,
        refs,
        broken,
        question,
        system_prompt=prompt.content,
        max_page_images=max_page_images,
        max_attachment_bytes=max_attachment_bytes,
    )

    async def on_text(delta: str) -> None:
        if on_reply is not None:
            await on_reply(REPLY_DELTA, {"text": delta, "attempt": 1})

    response = await client.create(
        [{"role": "user", "content": builder.content}],
        system=system,
        prompt_hash=prompt.hash,
        confirm_over_cap=confirm_over_cap,
        on_text=on_text if on_reply is not None else None,
    )
    model = response.model or client.model
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)
    await conversation.record(
        "context",
        model=client.model,
        prompt_hash=prompt.hash,
        detail={
            "reason": "explain",
            "section": section,
            "block": number,
            "sources": [ref.source_id for ref in refs if ref.source_id],
            "images": list(builder.images),
            "documents": list(builder.documents),
            "omitted": list(builder.omitted),
        },
    )
    await conversation.record(
        "user", message={"role": "user", "content": builder.record}, model=client.model
    )
    await conversation.record(
        "assistant",
        message=response.assistant_turn(),
        model=model,
        prompt_hash=prompt.hash,
        usage=response.usage.model_dump(),
    )
    if response.stop_reason == "refusal":
        raise RefusalError("the editor declined to explain the block")
    reply = response.text.strip()
    warning = None
    if not reply:
        warning = "El editor no ha dado ninguna explicación. Prueba a preguntar otra vez."
    elif response.stop_reason == "max_tokens":
        warning = "La explicación quedó cortada por el límite de longitud."
    result = ExplanationResult(
        subject=subject_slug,
        topic=topic_slug,
        section=section,
        block=number,
        block_text=block.text,
        question=question,
        reply=reply,
        refs=refs,
        images=list(builder.images),
        omitted=list(builder.omitted),
        warning=warning,
        model=model,
    )
    await conversation.record(
        EXPLANATION_RECORD,
        model=model,
        prompt_hash=prompt.hash,
        detail=result.model_dump(mode="json"),
    )
    if sync is not None:
        sync.note_change()
    return result


__all__ = [
    "PROMPT_NAME",
    "BlockAnchor",
    "BlockNotFoundError",
    "ExplanationResult",
    "assemble_explanation",
    "explain_block",
    "find_block",
]
