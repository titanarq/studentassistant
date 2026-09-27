"""The voice tutor: the student asks about a topic, the editor answers from the notes and sources.

Study mode (epic #13, #82). The student asks a question out loud -- "¿qué era la derivada?",
"ponme un ejemplo", "¿y eso por qué?" -- and the editor (`editor` role, prompt `editor_tutor`)
answers it, grounded in what the topic already has. Its input is the same one the revision chat
uses (`assemble_input`: the catalogue of citable sources, the sources, the transcript, the
decisions on the doubts and the current notes, a cached prefix), closed by the tutor's own
instruction, the last `HISTORY_TURNS` questions and answers (so a follow-up makes sense) and the
question.

The answer is Spanish plain text, short enough to be read aloud, streamed as it is written
(`on_reply("reply.delta", ...)`, as in `explain.py`). It cites the footnote labels of the current
notes (`[^p4]`) after what it takes from them; `refs` are those labels, in order of first
citation, that the notes define (any other label is dropped), so a client can list the sources and
strip the marks before speaking. Nothing of the notes changes.

**Conversation** `conversations/tutor.jsonl`, apart from the editor chat (`editor.jsonl`), whose
history and undo are not concerned: `context` (reason `tutor`), `user`, `assistant`, then one
`tutor.answer` record (the `TutorAnswer`) per question. `tutor_history` reads those back.

**Styles.** `style="spoken"` (the default) is the voice tutor above: the capture page's and the
Android app's. `style="written"` is the study screen's question chat (epic #332, #334): the prompt
`editor_study_chat`, a short answer in light Markdown (paragraphs, lists, bold; no headings) that
also cites the sections of the current notes it draws on as `[§anchor]`; `sections` are those
anchors, in order of first citation, that the notes have (`{anchor, title}`), and an anchor the
notes lack is left out and named in `warning`. Each style is given only its own earlier turns as
history. Neither style offers a tool nor writes anything under `notes/`: a request to change the
document is answered with "Eso se cambia en Construir: pídeselo allí al asistente." (a prompt
rule). Records written before the styles existed read as `spoken` with no `sections`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    DigestReader,
    EditorInput,
    assemble_input,
)
from studentassistant.editor.notes_format import (
    NotesDocument,
    ProvenanceError,
    parse,
    parse_provenance,
)
from studentassistant.editor.revise import (
    ChatRef,
    InvalidMessageError,
    NotesMissingError,
    ReplySink,
)
from studentassistant.llm import LLMClient, RefusalError, load_prompt
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    read_conversation,
    read_notes,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "editor_tutor"
WRITTEN_PROMPT_NAME = "editor_study_chat"
CONVERSATION_NAME = "tutor"
ANSWER_RECORD = "tutor.answer"
GENERATION_RECORD = "tutor.generation"
REPLY_DELTA = "reply.delta"
MAX_QUESTION_CHARS = 1000
HISTORY_TURNS = 6
"""How many earlier questions and answers the tutor is given."""

Clock = Callable[[], datetime]

TutorStyle = Literal["spoken", "written"]
"""How the tutor answers: read aloud (`spoken`) or in the study screen's chat (`written`)."""

TurnKind = Literal["answer", "generation"]
"""A tutor turn answers a question, or (study chat, #366) reports a generated study material."""

_REFERENCE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_SECTION_REFERENCE = re.compile(r"\[§[ \t]*#?([A-Za-z0-9][A-Za-z0-9_-]*)[ \t]*\]")

TUTOR_INSTRUCTION = (
    "## Tarea: responder como tutor a una pregunta del estudiante\n\n"
    "El estudiante está estudiando este tema y te pregunta en voz alta. A continuación tienes las"
    " preguntas y respuestas hasta ahora y la nueva pregunta. Contesta en texto plano, breve y"
    " para ser leído en voz alta, apoyándote en los apuntes y las fuentes y citando tras cada"
    " afirmación la nota al pie que usan los apuntes (`[^p4]`). No cambies los apuntes.\n"
)

WRITTEN_INSTRUCTION = (
    "## Tarea: responder por escrito a una pregunta del estudiante\n\n"
    "El estudiante está estudiando este tema con el documento delante y te pregunta por escrito"
    " en el chat de preguntas. A continuación tienes las preguntas y respuestas de este chat hasta"
    " ahora y la nueva pregunta. Contesta breve, en Markdown ligero (párrafos, listas, negrita; sin"
    " encabezados), apoyándote en los apuntes y las fuentes: cita cada sección de los apuntes que"
    " uses con su ancla (`[§causas]`) y, tras cada afirmación, la nota al pie que usan los apuntes"
    " (`[^p4]`). No cambies los apuntes: si te pide un cambio, contesta «Eso se cambia en"
    " Construir: pídeselo allí al asistente.»\n"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionRef(_Strict):
    """A section of the current notes an answer cites (`[§anchor]`)."""

    anchor: str
    title: str = Field(description="The section heading's title, as written (no `{#anchor}`).")


class TutorAnswer(_Strict):
    """One answer of the tutor; recorded as the `tutor.answer` conversation record."""

    subject: str
    topic: str
    style: TutorStyle = "spoken"
    question: str = Field(description="The student's question, as asked (trimmed).")
    reply: str = Field(description="The tutor's answer, Spanish, with the notes' `[^label]` marks.")
    refs: list[ChatRef] = Field(
        default_factory=list, description="The notes' footnotes the answer cites, in order."
    )
    sections: list[SectionRef] = Field(
        default_factory=list, description="The notes' sections the answer cites, in order."
    )
    warning: str | None = None
    model: str | None = None


class TutorGeneration(_Strict):
    """A study chat request that generated a material ("hazme un quiz", #366); recorded as the
    `tutor.generation` conversation record. The server matches and runs it; this only keeps it."""

    subject: str
    topic: str
    style: TutorStyle = "written"
    question: str = Field(description="The student's request, as typed (trimmed).")
    reply: str = Field(description="The Spanish sentence of the result.")
    option: str = Field(description="The study option it filled (`quiz`, `ejercicios`...).")
    material_kind: str = Field(description="The generator kind (`quiz`, `examen`...).")
    items: int = Field(ge=0, description="How many items the material has.")
    warnings: list[str] = Field(default_factory=list, description="The generation's, Spanish.")
    model: str | None = None


class TutorTurn(_Strict):
    """One question and its answer, as a client shows them."""

    time: datetime
    kind: TurnKind = "answer"
    style: TutorStyle = "spoken"
    question: str
    reply: str
    refs: list[ChatRef] = Field(default_factory=list)
    sections: list[SectionRef] = Field(default_factory=list)
    warning: str | None = None
    option: str | None = Field(default=None, description="`generation` turns: the study option.")
    items: int | None = Field(default=None, description="`generation` turns: the item count.")


class TutorHistory(_Strict):
    """The topic's tutor conversation, oldest first."""

    subject: str
    topic: str
    turns: list[TutorTurn] = Field(default_factory=list)


# -- the conversation ---------------------------------------------------------------------------


def _read_turns(vault: Vault, subject_slug: str, topic_slug: str) -> list[TutorTurn]:
    turns: list[TutorTurn] = []
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION_NAME):
        if record.kind == GENERATION_RECORD and record.detail:
            try:
                generation = TutorGeneration.model_validate(record.detail)
            except ValidationError:
                logger.warning(
                    "ignoring a malformed tutor record of %s/%s", subject_slug, topic_slug
                )
                continue
            turns.append(
                TutorTurn(
                    time=record.time,
                    kind="generation",
                    style=generation.style,
                    question=generation.question,
                    reply=generation.reply,
                    warning=" ".join(generation.warnings) or None,
                    option=generation.option,
                    items=generation.items,
                )
            )
            continue
        if record.kind != ANSWER_RECORD or not record.detail:
            continue
        try:
            answer = TutorAnswer.model_validate(record.detail)
        except ValidationError:
            logger.warning("ignoring a malformed tutor record of %s/%s", subject_slug, topic_slug)
            continue
        turns.append(
            TutorTurn(
                time=record.time,
                style=answer.style,
                question=answer.question,
                reply=answer.reply,
                refs=answer.refs,
                sections=answer.sections,
                warning=answer.warning,
            )
        )
    return turns


def tutor_history(vault: Vault, subject_slug: str, topic_slug: str) -> TutorHistory:
    """The topic's tutor turns, oldest first; reads only (blocking).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
    """
    return TutorHistory(
        subject=subject_slug,
        topic=topic_slug,
        turns=_read_turns(vault, subject_slug, topic_slug),
    )


async def _record(
    vault: Vault, subject_slug: str, topic_slug: str, clock: Clock, kind: str, **fields: Any
) -> None:
    entry = ConversationRecord(time=clock(), kind=kind, **fields)
    try:
        await asyncio.to_thread(
            append_conversation_record, vault, subject_slug, topic_slug, CONVERSATION_NAME, entry
        )
    except Exception:
        logger.exception(
            "could not record the tutor conversation of %s/%s", subject_slug, topic_slug
        )


async def record_generation(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    generation: TutorGeneration,
    *,
    sync: GitSync | None = None,
    clock: Clock = _utc_now,
) -> None:
    """Append a generation turn of the study chat to the tutor conversation, so `tutor_history`
    shows it (and a later written question has it as history). `sync`, when given, is told the
    conversation changed. A failure to record is logged, never raised."""
    await _record(
        vault,
        subject_slug,
        topic_slug,
        clock,
        GENERATION_RECORD,
        model=generation.model,
        detail=generation.model_dump(mode="json"),
    )
    if sync is not None:
        sync.note_change()


# -- the refs -----------------------------------------------------------------------------------


def cited_refs(document: NotesDocument, reply: str) -> list[ChatRef]:
    """The notes' footnotes `reply` cites (`[^label]`), in order of first citation.

    A label the notes do not define, or whose definition names no source, is left out.
    """
    definitions = {d.label: d for d in reversed(document.footnotes)}  # the first one wins
    refs: list[ChatRef] = []
    seen: set[str] = set()
    for match in _REFERENCE.finditer(reply):
        label = match[1]
        if label in seen:
            continue
        seen.add(label)
        definition = definitions.get(label)
        if definition is None:
            continue
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            continue
        refs.append(
            ChatRef(
                label=label,
                kind=provenance.kind,
                text=provenance.text,
                source_id=provenance.source_id,
                path=provenance.path,
            )
        )
    return refs


def cited_sections(document: NotesDocument, reply: str) -> tuple[list[SectionRef], list[str]]:
    """The notes' sections `reply` cites (`[§anchor]`), in order of first citation, and the
    anchors it cites that the notes lack (in the same order); each anchor counted once."""
    titles: dict[str, str] = {}
    for section in document.sections:
        if section.anchor is not None:
            titles.setdefault(section.anchor, section.heading.title)
    sections: list[SectionRef] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for match in _SECTION_REFERENCE.finditer(reply):
        anchor = match[1]
        if anchor in seen:
            continue
        seen.add(anchor)
        if anchor in titles:
            sections.append(SectionRef(anchor=anchor, title=titles[anchor]))
        else:
            unknown.append(anchor)
    return sections, unknown


def _unknown_sections_warning(unknown: list[str]) -> str:
    names = ", ".join(f"§{anchor}" for anchor in unknown)
    if len(unknown) == 1:
        return f"La respuesta cita una sección que no está en los apuntes: {names}."
    return f"La respuesta cita secciones que no están en los apuntes: {names}."


# -- the turn -----------------------------------------------------------------------------------


def _history_text(turns: list[TutorTurn]) -> str:
    if not turns:
        return "(Es la primera pregunta.)"
    lines: list[str] = []
    for turn in turns[-HISTORY_TURNS:]:
        lines.append(f"Estudiante: {turn.question}")
        lines.append(f"Tutor: {turn.reply or '(sin respuesta)'}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _turn_text(turns: list[TutorTurn], question: str, style: TutorStyle) -> str:
    heard = "escrita" if style == "written" else "reconocimiento de voz"
    return (
        "## Preguntas y respuestas hasta ahora\n\n"
        f"{_history_text(turns)}\n\n"
        f"## Nueva pregunta del estudiante ({heard})\n\n"
        f"{question}\n"
    )


async def ask_tutor(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    question: str,
    *,
    client: LLMClient,
    style: TutorStyle = "spoken",
    sync: GitSync | None = None,
    on_reply: ReplySink | None = None,
    digest: DigestReader | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> TutorAnswer:
    """Answer one question of the student from the topic's notes and sources (see the module
    docstring), in `style` (`spoken`, read aloud, or `written`, the study chat). `client` is the
    caller's (role `editor`, or the one `[editor] study_chat_role` names for the written chat).
    `sync`, when given, is told the conversation changed (the sync loop commits it).

    Raises:
        InvalidMessageError: an empty or too long question; nothing sent.
        NotesMissingError: the topic has no notes yet; nothing sent.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing
            written but the conversation records of the call.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    text = " ".join(question.split())
    if not text:
        raise InvalidMessageError("Dime qué quieres preguntar.")
    if len(text) > MAX_QUESTION_CHARS:
        raise InvalidMessageError(
            f"La pregunta es demasiado larga (más de {MAX_QUESTION_CHARS} caracteres)."
        )
    notes = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    if not notes or not notes.strip():
        raise NotesMissingError(
            "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."
        )
    turns = [
        t
        for t in await asyncio.to_thread(_read_turns, vault, subject_slug, topic_slug)
        if t.style == style
    ]
    written = style == "written"
    prompt = load_prompt(WRITTEN_PROMPT_NAME if written else PROMPT_NAME)
    assembled: EditorInput = await asyncio.to_thread(
        assemble_input,
        vault,
        subject_slug,
        topic_slug,
        prompt=prompt,
        digest=digest,
        max_page_images=max_page_images,
        max_attachment_bytes=max_attachment_bytes,
        instruction=WRITTEN_INSTRUCTION if written else TUTOR_INSTRUCTION,
    )
    turn = {"type": "text", "text": _turn_text(turns, text, style)}

    async def on_text(delta: str) -> None:
        if on_reply is not None:
            await on_reply(REPLY_DELTA, {"text": delta, "attempt": 1})

    response = await client.create(
        [{"role": "user", "content": [*assembled.content, turn]}],
        system=assembled.system,
        prompt_hash=prompt.hash,
        confirm_over_cap=confirm_over_cap,
        on_text=on_text if on_reply is not None else None,
    )
    model = response.model or client.model

    async def record(kind: str, **fields: Any) -> None:
        await _record(vault, subject_slug, topic_slug, clock, kind, **fields)

    await record(
        "context",
        model=client.model,
        prompt_hash=prompt.hash,
        detail={"reason": "tutor", "style": style, **assembled.summary()},
    )
    await record(
        "user",
        message={"role": "user", "content": [*assembled.record_content, turn]},
        model=client.model,
    )
    await record(
        "assistant",
        message=response.assistant_turn(),
        model=model,
        prompt_hash=prompt.hash,
        usage=response.usage.model_dump(),
    )
    if response.stop_reason == "refusal":
        raise RefusalError("the tutor declined to answer the question")
    reply = response.text.strip()
    document = parse(notes)
    warnings: list[str] = []
    if not reply:
        warnings.append("El tutor no ha contestado. Prueba a preguntar otra vez.")
    elif response.stop_reason == "max_tokens":
        warnings.append("La respuesta quedó cortada por el límite de longitud.")
    sections: list[SectionRef] = []
    if written:
        sections, unknown = cited_sections(document, reply)
        if unknown:
            warnings.append(_unknown_sections_warning(unknown))
    answer = TutorAnswer(
        subject=subject_slug,
        topic=topic_slug,
        style=style,
        question=text,
        reply=reply,
        refs=cited_refs(document, reply),
        sections=sections,
        warning=" ".join(warnings) or None,
        model=model,
    )
    await record(
        ANSWER_RECORD, model=model, prompt_hash=prompt.hash, detail=answer.model_dump(mode="json")
    )
    if sync is not None:
        sync.note_change()
    return answer


__all__ = [
    "ANSWER_RECORD",
    "CONVERSATION_NAME",
    "GENERATION_RECORD",
    "MAX_QUESTION_CHARS",
    "PROMPT_NAME",
    "WRITTEN_PROMPT_NAME",
    "SectionRef",
    "TurnKind",
    "TutorAnswer",
    "TutorGeneration",
    "TutorHistory",
    "TutorStyle",
    "TutorTurn",
    "ask_tutor",
    "cited_refs",
    "cited_sections",
    "record_generation",
    "tutor_history",
]
