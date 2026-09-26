"""Revising the notes in conversation: a chat reply plus section edit ops, validated and committed.

The student talks to the editor (`editor` role, Opus) about notes it already wrote -- "demasiado
resumido", "pon un ejemplo", "no inventes", "usa la explicación del libro" -- and each turn is:

1. **The reply**: the editor first writes a Spanish answer as plain text, which streams to the
   caller as it is written (`on_reply("reply.delta", ...)`, through
   `LLMClient.create(on_text=...)`).
2. **The change**: then, when something must change, it calls the strict tool `apply_edits`
   (`EditsOutput`) once: the edit ops of `edits.py` (`replace_block`, `insert_after`,
   `delete_block`, `replace_section`, `move_section`, `add_section`) and the footnotes they need, a
   one-sentence `summary`, and the standing instructions the student gave -- the topic's
   `fidelity_mode` ("no inventes nada" -> `estricto`) --, plus `proposed_style_rules` when an
   instruction looks general ("me gustan las tablas para comparar", "siempre un ejemplo": a rule for
   the subject's style guide, `style_guide.py`, written only once the student confirms it) and
   `confirmed_style_rules` when the student confirms in the chat a rule proposed in an earlier turn.
   A turn with no tool call is a chat-only answer.

The change is checked before anything is written: the ops must apply (`apply_edits`), and the
edited notes must pass the provenance validator (`notes_format.validate`) in the fidelity mode the
turn leaves. A failing change is sent back with the Spanish errors (a `tool_result` error), at most
`MAX_REASKS` times, and the reply starts again (`on_reply("reply.restart", ...)`); past that nothing
is applied and the result carries a warning. A valid change is written through the vault
(`write_notes`, `set_fidelity_mode`, `style_guide.append_rules`) and committed at once with a
Spanish message (`Apuntes de <s>/<t> revisados: <summary>`); the result carries a unified diff of
`apuntes.md`, the sections touched and the paths the commit changed, and a `notes.edited` event
goes to `on_event` (the server publishes it on the bus).

**The latest version**: no lock is held across the Claude call, so the student may save the notes
(`direct_edit.save_student_edit`) while the editor answers. The turn remembers the notes its block
map was built from and applies its change under the topic's short write lock (`notes_lock`), which
the student's save takes too; when the notes changed meanwhile, the ops (numbered against the old
notes) are not applied: the editor is re-asked with the new block map and a Spanish note ("el
estudiante ha cambiado los apuntes mientras respondías"), which counts against `MAX_REASKS`.

**A growing document**: a topic with no `apuntes.md` yet is revised from `# <topic title>`, so the
first turn can create the notes (with `add_section`).

**Undo** (`undo_last_revision`): the latest applied turn not yet undone is reverted with
`GitSync.revert_paths` -- a `git revert` of that commit restricted to the files the turn changed,
so the ledger and conversation lines it also carried stay -- and committed (`Deshecho en <s>/<t>:
<summary>`). Refused when one of those files changed afterwards (a later turn must be undone
first; a regeneration cannot be undone this way). Undoing again goes one more turn back.

**Conversation**: every call is recorded in `conversations/editor.jsonl` like the generation's
(`context` with reason `revise`, `user`, `assistant`, `validation`), each turn as a `revision`
record (the `RevisionResult`) and each undo as `notes.undone`; `chat_history` reads them back, and
the last `HISTORY_TURNS` turns -- with the student's own edits of the document (`student_edit`
records of `direct_edit.py`) among them -- are given to the editor as the conversation so far.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.editor.edits import (
    EditError,
    EditOp,
    NewFootnote,
    apply_edits,
    describe_sections,
)
from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    DigestReader,
    EditorInput,
    assemble_input,
)
from studentassistant.editor.notes_format import (
    FidelityMode,
    notes_revision,
    topic_source_resolver,
    validate,
)
from studentassistant.editor.notes_lock import holding_notes
from studentassistant.editor.style_guide import (
    append_rules,
    new_rules,
    normalize_rule,
    read_style_guide,
    rule_errors,
)
from studentassistant.llm import (
    LLMClient,
    LLMResponse,
    RefusalError,
    load_prompt,
    strict_tool,
)
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    RevertConflictError,
    Vault,
    append_conversation_record,
    notes_path,
    read_conversation,
    read_notes,
    set_fidelity_mode,
    subject_directory,
    topic_directory,
    write_notes,
)
from studentassistant.vault.subjects import SUBJECT_FILE_NAME
from studentassistant.vault.topics import TOPIC_FILE_NAME

logger = logging.getLogger(__name__)

PROMPT_NAME = "editor_revise"
CONVERSATION_NAME = "editor"
EDIT_TOOL = "apply_edits"
NOTES_EDITED_KIND = "notes.edited"
NOTES_UNDONE_KIND = "notes.undone"
REVISION_RECORD = "revision"
STUDENT_EDIT_RECORD = "student_edit"
"""The conversation record of a save of the student's own edit (`direct_edit.py`)."""
STUDENT_EDIT_DIFF_CHARS = 3000
"""How much of a student edit's diff the editor is shown in the conversation."""
NOTES_CHANGED_NOTE = "el estudiante ha cambiado los apuntes mientras respondías"
EXPLANATION_RECORD = "explanation"
"""The conversation record of a "¿Por qué pusiste esto?" answer (`explain.py`)."""
MAX_REASKS = 2
"""How many times a change that fails the checks is sent back to the editor."""
MAX_MESSAGE_CHARS = 4000
HISTORY_TURNS = 12
"""How many earlier turns of the conversation the editor is given."""
MAX_STYLE_RULES = 5
"""The most style rules one turn may propose (or confirm)."""

Clock = Callable[[], datetime]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
ReplySink = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Receives `reply.delta` (`text`, `attempt`) and `reply.restart` (`attempt`) while streaming."""

REPLY_DELTA = "reply.delta"
REPLY_RESTART = "reply.restart"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- errors --------------------------------------------------------------------------------------


class RevisionError(ValueError):
    """A revision request that cannot be carried out; the message is Spanish, for the student."""


class InvalidMessageError(RevisionError):
    """The student's message is empty or too long."""


class NotesMissingError(RevisionError):
    """The topic has no `notes/apuntes.md` yet (for the callers that need one; a revision turn
    starts the notes instead)."""


class NothingToUndoError(RevisionError):
    """No applied turn is left to undo."""


class UndoConflictError(RevisionError):
    """A file the turn changed was changed again afterwards."""


# -- models ---------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditsOutput(_Strict):
    """The input of the `apply_edits` tool: everything one turn changes."""

    ops: list[EditOp] = Field(default_factory=list)
    footnotes: list[NewFootnote] = Field(
        default_factory=list, description="Footnote definitions the new texts need."
    )
    summary: str = Field(description="What this turn changes, one short Spanish sentence.")
    fidelity_mode: Literal["estricto", "ampliado"] | None = Field(
        default=None, description="Only when the student sets the topic's fidelity mode."
    )
    proposed_style_rules: list[str] = Field(
        default_factory=list,
        description="Rules proposed for the subject's style guide when an instruction looks"
        " general, one Spanish sentence each; the student confirms them later.",
    )
    confirmed_style_rules: list[str] = Field(
        default_factory=list,
        description="Rules proposed in an earlier turn that the student now confirms, copied"
        " exactly.",
    )


class RevisionResult(_Strict):
    """What one turn of the conversation did; recorded as the `revision` conversation record."""

    subject: str
    topic: str
    message: str = Field(description="The student's message.")
    reply: str = Field(description="The editor's reply, Spanish.")
    applied: bool = Field(description="True when the turn changed the notes or an instruction.")
    summary: str | None = None
    ops: list[EditOp] = Field(default_factory=list)
    footnotes: list[NewFootnote] = Field(default_factory=list)
    fidelity_mode: str | None = Field(
        default=None, description="The topic's new fidelity mode, when the turn changed it."
    )
    style_rules: list[str] = Field(
        default_factory=list,
        description="The rules this turn added to the subject's style guide (confirmed ones).",
    )
    proposed_style_rules: list[str] = Field(
        default_factory=list,
        description="Rules the editor proposes for the subject's style guide, not written until"
        " the student confirms them.",
    )
    notes_changed: bool = False
    changed_sections: list[str] = Field(default_factory=list)
    diff: str = Field(default="", description="Unified diff of notes/apuntes.md.")
    notes: str | None = Field(default=None, description="The new notes, when they changed.")
    paths: list[str] = Field(default_factory=list, description="Vault paths the commit changed.")
    commit: str | None = None
    revision: str | None = Field(
        default=None,
        description="The revision (`notes_revision`) of `apuntes.md` after the turn; `None` while"
        " the topic has no notes.",
    )
    attempts: int = 0
    errors: list[str] = Field(default_factory=list, description="The last check's errors.")
    warning: str | None = None
    model: str | None = None


class UndoResult(_Strict):
    """What undoing one turn did."""

    subject: str
    topic: str
    undone_commit: str
    summary: str | None = None
    commit: str | None = None
    notes_changed: bool = False
    diff: str = ""
    notes: str | None = None
    paths: list[str] = Field(default_factory=list)
    revision: str | None = Field(
        default=None, description="The revision of `apuntes.md` after the undo."
    )


class ChatRef(_Strict):
    """A source a "¿Por qué pusiste esto?" answer points to: one footnote of the block."""

    label: str = Field(description="The footnote label in the notes (`p1`), for the sources panel.")
    kind: str = Field(description="`notes`, `book`, `pdf`, `web`, `transcript` or `ia`.")
    text: str = Field(description="The footnote's text, e.g. «Apuntes, página 1».")
    source_id: str | None = None
    path: str | None = Field(default=None, description="Topic-relative file of the source.")


class ChatTurn(_Strict):
    """One turn of the conversation as the web chat shows it.

    `kind` is `revise` for a revision turn and `explain` for a "¿Por qué pusiste esto?" answer,
    whose `refs` are the sources of the block it explains; `student_edit`, a save of the student's
    own edit of the document (its `summary` holds the diff), is only in the editor's history.
    """

    time: datetime
    kind: Literal["revise", "explain", "student_edit"] = "revise"
    message: str
    reply: str
    applied: bool = False
    summary: str | None = None
    changed_sections: list[str] = Field(default_factory=list)
    commit: str | None = None
    undone: bool = False
    warning: str | None = None
    refs: list[ChatRef] = Field(default_factory=list)
    proposed_style_rules: list[str] = Field(
        default_factory=list,
        description="The turn's proposed style rules the subject's guide does not have yet.",
    )


class _ExplanationView(BaseModel):
    """What the chat reads of an `explanation` record."""

    question: str
    reply: str
    refs: list[ChatRef] = Field(default_factory=list)
    warning: str | None = None


class ChatHistory(_Strict):
    """The topic's revision conversation; `can_undo` says whether an applied turn can be undone."""

    subject: str
    topic: str
    turns: list[ChatTurn] = Field(default_factory=list)
    can_undo: bool = False


# -- the conversation file ------------------------------------------------------------------------


class _Conversation:
    def __init__(self, vault: Vault, subject_slug: str, topic_slug: str, clock: Clock) -> None:
        self.vault, self.subject, self.topic, self.clock = vault, subject_slug, topic_slug, clock

    async def record(self, kind: str, **fields: Any) -> None:
        entry = ConversationRecord(time=self.clock(), kind=kind, **fields)
        try:
            await asyncio.to_thread(
                append_conversation_record,
                self.vault,
                self.subject,
                self.topic,
                CONVERSATION_NAME,
                entry,
            )
        except Exception:
            logger.exception(
                "could not record the editor conversation of %s/%s", self.subject, self.topic
            )


def _read_turns(
    vault: Vault, subject_slug: str, topic_slug: str, *, student_edits: bool = False
) -> list[ChatTurn]:
    turns: list[ChatTurn] = []
    undone: set[str] = set()
    guide = read_style_guide(vault, subject_slug).rules
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION_NAME):
        if record.kind == NOTES_UNDONE_KIND and record.detail:
            commit = record.detail.get("undone_commit")
            if isinstance(commit, str):
                undone.add(commit)
        if student_edits and record.kind == STUDENT_EDIT_RECORD and record.detail:
            sections = record.detail.get("changed_sections")
            diff = record.detail.get("diff")
            turns.append(
                ChatTurn(
                    time=record.time,
                    kind="student_edit",
                    message="",
                    reply="",
                    applied=True,
                    summary=diff if isinstance(diff, str) else None,
                    changed_sections=[str(a) for a in sections]
                    if isinstance(sections, list)
                    else [],
                    commit=record.detail.get("commit")
                    if isinstance(record.detail.get("commit"), str)
                    else None,
                )
            )
            continue
        if record.kind == EXPLANATION_RECORD and record.detail:
            try:
                view = _ExplanationView.model_validate(record.detail)
            except ValidationError:
                logger.warning(
                    "ignoring a malformed explanation record of %s/%s", subject_slug, topic_slug
                )
                continue
            turns.append(
                ChatTurn(
                    time=record.time,
                    kind="explain",
                    message=view.question,
                    reply=view.reply,
                    warning=view.warning,
                    refs=view.refs,
                )
            )
            continue
        if record.kind != REVISION_RECORD or not record.detail:
            continue
        try:
            result = RevisionResult.model_validate(record.detail)
        except ValidationError:
            logger.warning(
                "ignoring a malformed revision record of %s/%s", subject_slug, topic_slug
            )
            continue
        turns.append(
            ChatTurn(
                time=record.time,
                message=result.message,
                reply=result.reply,
                applied=result.applied,
                summary=result.summary,
                changed_sections=result.changed_sections,
                commit=result.commit,
                warning=result.warning,
                proposed_style_rules=new_rules(guide, result.proposed_style_rules),
            )
        )
    return [
        turn.model_copy(update={"undone": turn.commit is not None and turn.commit in undone})
        for turn in turns
    ]


def _undo_target(vault: Vault, subject_slug: str, topic_slug: str) -> RevisionResult | None:
    """The latest applied, committed turn not undone yet."""
    undone: set[str] = set()
    candidates: list[RevisionResult] = []
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION_NAME):
        if record.kind == NOTES_UNDONE_KIND and record.detail:
            commit = record.detail.get("undone_commit")
            if isinstance(commit, str):
                undone.add(commit)
        elif record.kind == REVISION_RECORD and record.detail:
            try:
                result = RevisionResult.model_validate(record.detail)
            except ValidationError:
                continue
            if result.applied and result.commit and result.paths:
                candidates.append(result)
    remaining = [result for result in candidates if result.commit not in undone]
    return remaining[-1] if remaining else None


def chat_history(vault: Vault, subject_slug: str, topic_slug: str) -> ChatHistory:
    """The topic's revision turns, oldest first; reads only (blocking).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
    """
    turns = _read_turns(vault, subject_slug, topic_slug)
    return ChatHistory(
        subject=subject_slug,
        topic=topic_slug,
        turns=turns,
        can_undo=_undo_target(vault, subject_slug, topic_slug) is not None,
    )


# -- the turn --------------------------------------------------------------------------------------


REVISE_INSTRUCTION = (
    "## Tarea: revisar los apuntes con el estudiante (herramienta `apply_edits`)\n\n"
    "A continuación tienes la conversación hasta ahora, el mapa de bloques de los apuntes"
    " actuales y el nuevo mensaje del estudiante. Contesta primero con tu respuesta en texto y,"
    " si hay que cambiar algo, llama después una vez a `apply_edits` con todos los cambios de"
    " este turno.\n"
)


def _history_text(turns: list[ChatTurn]) -> str:
    if not turns:
        return "(Es el primer mensaje de la conversación.)"
    lines: list[str] = []
    for turn in turns[-HISTORY_TURNS:]:
        if turn.kind == "student_edit":
            where = ", ".join(f"#{anchor}" for anchor in turn.changed_sections) or "el documento"
            lines.append(f"[El estudiante editó él mismo los apuntes ({where}). Cambios:]")
            diff = turn.summary or ""
            if len(diff) > STUDENT_EDIT_DIFF_CHARS:
                diff = diff[:STUDENT_EDIT_DIFF_CHARS].rstrip() + "\n…"
            lines.append(diff.rstrip() or "(sin diferencias)")
            lines.append("")
            continue
        lines.append(f"Estudiante: {turn.message}")
        reply = turn.reply or "(sin respuesta)"
        lines.append(f"Editor: {reply}")
        if turn.applied:
            state = " (el estudiante lo deshizo)" if turn.undone else ""
            lines.append(f"[Cambio aplicado: {turn.summary or 'sin resumen'}{state}]")
        elif turn.warning and turn.kind == "revise":
            lines.append("[No se aplicó ningún cambio: no pasó la validación.]")
        if turn.proposed_style_rules:
            rules = "; ".join(f"«{rule}»" for rule in turn.proposed_style_rules)
            lines.append(f"[Propuesto para la guía de estilo, sin confirmar aún: {rules}]")
        lines.append("")
    return "\n".join(lines).rstrip()


def _turn_text(turns: list[ChatTurn], notes: str, mode: str, message: str) -> str:
    return (
        "## Conversación hasta ahora\n\n"
        f"{_history_text(turns)}\n\n"
        f"## Mapa de bloques de los apuntes actuales (modo de fidelidad «{mode}»)\n\n"
        f"{describe_sections(notes)}\n\n"
        "## Nuevo mensaje del estudiante\n\n"
        f"{message}\n"
    )


def _stale_turn(response: LLMResponse, notes: str, mode: str) -> dict[str, Any]:
    """The re-ask after the notes changed under the turn: the new block map, apply again."""
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
            "text": f"{reason}\n\n## Mapa de bloques de los apuntes actuales (modo de fidelidad"
            f" «{mode}»)\n\n{describe_sections(notes)}\n\nRespeta lo que ha escrito el"
            " estudiante: escribe otra vez una respuesta breve y llama a"
            f" `{EDIT_TOOL}` con el cambio completo sobre estos apuntes.",
        }
    )
    return {"role": "user", "content": content}


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


def _parse_call(response: LLMResponse) -> tuple[EditsOutput | None, list[str]]:
    """The tool input (None without a call) and the errors of a malformed one."""
    calls = [call for call in response.tool_calls if call.name == EDIT_TOOL]
    if not calls:
        return None, []
    if response.stop_reason == "max_tokens":
        return None, ["La llamada quedó cortada por el límite de longitud: da un cambio más corto."]
    if len(calls) > 1:
        return None, [f"Llama a `{EDIT_TOOL}` una sola vez, con todos los cambios del turno."]
    try:
        value = EditsOutput.model_validate(json.loads(calls[0].input_json))
    except (json.JSONDecodeError, ValidationError) as error:
        return None, [f"La entrada de `{EDIT_TOOL}` no es válida: {error}"]
    return value, []


def _rules(rules: list[str]) -> list[str]:
    return [rule for rule in (normalize_rule(rule) for rule in rules) if rule]


def _pending_rules(turns: list[ChatTurn]) -> list[str]:
    """Rules proposed in earlier turns and still not in the guide: what may be confirmed."""
    return new_rules([], [rule for turn in turns for rule in turn.proposed_style_rules])


def _check(
    value: EditsOutput,
    notes: str,
    assembled: EditorInput,
    vault: Vault,
    pending: list[str],
) -> tuple[list[str], str | None]:
    """`(errors, edited notes)` of a change."""
    errors: list[str] = []
    changes = value.ops or value.fidelity_mode is not None or value.confirmed_style_rules
    if changes and not value.summary.strip():
        errors.append("Falta el resumen (`summary`) del cambio.")
    for field in ("proposed_style_rules", "confirmed_style_rules"):
        rules = _rules(getattr(value, field))
        if len(rules) > MAX_STYLE_RULES:
            errors.append(f"Da como mucho {MAX_STYLE_RULES} reglas en `{field}`.")
        errors.extend(rule_errors(rules))
    known = {rule.casefold() for rule in pending}
    for rule in _rules(value.confirmed_style_rules):
        if rule.casefold() not in known:
            errors.append(
                f"«{rule[:60]}» no es una regla propuesta antes y sin confirmar: en"
                " `confirmed_style_rules` copia exactamente una regla que propusiste en un turno"
                " anterior; una regla nueva va en `proposed_style_rules`."
            )
    try:
        edited = apply_edits(notes, value.ops, value.footnotes)
    except EditError as error:
        return [*errors, *error.errors], None
    mode: FidelityMode = value.fidelity_mode or assembled.fidelity_mode
    resolver = topic_source_resolver(vault, assembled.subject_slug, assembled.topic_slug)
    errors.extend(validate(edited, mode, resolver))
    return errors, edited


def _diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="apuntes.md (antes)",
            tofile="apuntes.md",
        )
    )


def _changed_sections(ops: list[EditOp]) -> list[str]:
    anchors = (op.anchor if op.op == "add_section" else op.section for op in ops)
    return list(dict.fromkeys((anchor or "").strip().removeprefix("#") for anchor in anchors))


def _apply(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    *,
    notes: str,
    edited: str,
    value: EditsOutput,
    current_mode: str,
) -> tuple[list[str], str | None, str | None, list[str]]:
    """Write and commit one change; blocking. `(paths, commit, new mode, rules added)`."""
    root = vault.path
    paths: list[str] = []
    if edited != notes:
        write_notes(vault, subject_slug, topic_slug, edited)
        paths.append(notes_path(vault, subject_slug, topic_slug).relative_to(root).as_posix())
    new_mode = None
    if value.fidelity_mode is not None and value.fidelity_mode != current_mode:
        set_fidelity_mode(vault, subject_slug, topic_slug, value.fidelity_mode)
        new_mode = value.fidelity_mode
        topic_file = topic_directory(vault, subject_slug, topic_slug) / TOPIC_FILE_NAME
        paths.append(topic_file.relative_to(root).as_posix())
    added = append_rules(vault, subject_slug, _rules(value.confirmed_style_rules))
    if added:
        subject_file = subject_directory(vault, subject_slug) / SUBJECT_FILE_NAME
        paths.append(subject_file.relative_to(root).as_posix())
    if not paths:
        return [], None, None, []
    sync.note_change()
    commit = sync.checkpoint(
        f"Apuntes de {subject_slug}/{topic_slug} revisados: {_short(value.summary)}"
    )
    return paths, commit, new_mode, added


_Applied = tuple[list[str], str | None, str | None, list[str]]


def _apply_if_current(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    *,
    base: str | None,
    notes: str,
    edited: str,
    value: EditsOutput,
    current_mode: str,
) -> tuple[_Applied | None, str | None]:
    """Apply the change under the write lock when the notes are still `base`; blocking.

    `(applied, current)`: `applied` is `_apply`'s result, or `None` when the stored notes are no
    longer `base` (nothing written); `current` is the stored notes read under the lock.
    """
    with holding_notes(vault, subject_slug, topic_slug):
        current = read_notes(vault, subject_slug, topic_slug)
        if current != base:
            return None, current
        applied = _apply(
            vault,
            sync,
            subject_slug,
            topic_slug,
            notes=notes,
            edited=edited,
            value=value,
            current_mode=current_mode,
        )
        return applied, current


def _seeded(stored: str | None, title: str) -> str:
    """The notes a turn works on: the stored ones, or `# <title>` when there are none yet."""
    if stored is not None and stored.strip():
        return stored
    return f"# {' '.join(title.split()) or 'Apuntes'}\n"


async def revise_notes(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    message: str,
    *,
    client: LLMClient,
    sync: GitSync,
    on_reply: ReplySink | None = None,
    on_event: EventSink | None = None,
    digest: DigestReader | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> RevisionResult:
    """One turn of the revision conversation (see the module docstring).

    Raises:
        InvalidMessageError: an empty or too long message; nothing sent.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing
            written but the conversation records of the calls made.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    text = message.strip()
    if not text:
        raise InvalidMessageError("Escribe qué quieres cambiar en los apuntes.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidMessageError(
            f"El mensaje es demasiado largo (más de {MAX_MESSAGE_CHARS} caracteres)."
        )
    base = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    turns = await asyncio.to_thread(
        lambda: _read_turns(vault, subject_slug, topic_slug, student_edits=True)
    )
    prompt = load_prompt(PROMPT_NAME)
    assembled: EditorInput = await asyncio.to_thread(
        assemble_input,
        vault,
        subject_slug,
        topic_slug,
        prompt=prompt,
        digest=digest,
        max_page_images=max_page_images,
        max_attachment_bytes=max_attachment_bytes,
        instruction=REVISE_INSTRUCTION,
    )
    notes = _seeded(base, assembled.topic_title)
    turn = {"type": "text", "text": _turn_text(turns, notes, assembled.fidelity_mode, text)}
    messages: list[dict[str, Any]] = [{"role": "user", "content": [*assembled.content, turn]}]
    tool = strict_tool(
        EDIT_TOOL,
        "Apply this turn's changes to the notes: edit ops, new footnotes, a summary, and the"
        " standing instructions the student gave.",
        EditsOutput,
    )
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)

    model = client.model
    value: EditsOutput | None = None
    edited: str | None = None
    applied: _Applied | None = None
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
                detail={"reason": "revise", **assembled.summary()},
            )
            await conversation.record(
                "user",
                message={"role": "user", "content": [*assembled.record_content, turn]},
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
            raise RefusalError("the editor declined to revise the notes")
        reply = response.text.strip()
        value, errors = _parse_call(response)
        stale = False
        if value is not None:
            errors, edited = await asyncio.to_thread(
                _check, value, notes, assembled, vault, _pending_rules(turns)
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
                    notes=notes,
                    edited=edited,
                    value=value,
                    current_mode=assembled.fidelity_mode,
                )
            )
            if applied is None:
                stale = True
                base, notes = current, _seeded(current, assembled.topic_title)
                errors = [f"No se ha aplicado: {NOTES_CHANGED_NOTE}."]
        await conversation.record("validation", detail={"attempt": attempt, "errors": errors})
        if not errors or attempt > MAX_REASKS:
            break
        reask = (
            _stale_turn(response, notes, assembled.fidelity_mode)
            if stale
            else _reask_turn(response, errors)
        )
        messages = [*messages, response.assistant_turn(), reask]
        await conversation.record("user", message=reask, model=model)

    result = RevisionResult(
        subject=subject_slug,
        topic=topic_slug,
        message=text,
        reply=reply,
        applied=False,
        attempts=attempts,
        model=model,
    )
    if errors:
        warning = (
            "El editor no ha podido aplicar el cambio porque has cambiado los apuntes mientras"
            " respondía; los apuntes se quedan como los dejaste. Vuelve a pedírselo."
            if stale
            else "El editor no ha podido aplicar el cambio cumpliendo las reglas de"
            " procedencia; los apuntes no han cambiado. Prueba a pedirlo de otra forma."
        )
        result = result.model_copy(update={"errors": errors, "warning": warning})
    elif value is not None and edited is not None and applied is not None:
        paths, commit, new_mode, added = applied
        changed = edited != notes
        guide = await asyncio.to_thread(read_style_guide, vault, subject_slug)
        result = result.model_copy(
            update={
                "reply": reply or value.summary.strip(),
                "applied": bool(paths),
                "summary": value.summary.strip() or None,
                "ops": value.ops,
                "footnotes": value.footnotes,
                "fidelity_mode": new_mode,
                "style_rules": added,
                "proposed_style_rules": new_rules(guide.rules, _rules(value.proposed_style_rules)),
                "notes_changed": changed,
                "changed_sections": _changed_sections(value.ops) if changed else [],
                "diff": _diff(notes, edited) if changed else "",
                "notes": edited if changed else None,
                "paths": paths,
                "commit": commit,
            }
        )
    if result.notes_changed and result.notes is not None:
        revision: str | None = notes_revision(result.notes)
    else:
        stored = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
        revision = None if stored is None else notes_revision(stored)
    result = result.model_copy(update={"revision": revision})
    payload = result.model_dump(mode="json")
    await conversation.record(REVISION_RECORD, model=model, prompt_hash=prompt.hash, detail=payload)
    sync.note_change()  # the conversation's last lines, committed by the sync loop
    if result.applied:
        await _emit(on_event, NOTES_EDITED_KIND, _event_payload(payload), subject_slug, topic_slug)
    return result


def _event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The bus event: the result without the whole notes text."""
    return {key: value for key, value in payload.items() if key != "notes"}


async def _emit(
    on_event: EventSink | None, kind: str, payload: dict[str, Any], subject: str, topic: str
) -> None:
    if on_event is None:
        return
    try:
        await on_event(kind, payload)
    except Exception:
        logger.exception("could not publish %s for %s/%s", kind, subject, topic)


# -- undo ------------------------------------------------------------------------------------------


async def undo_last_revision(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    sync: GitSync,
    on_event: EventSink | None = None,
    clock: Clock = _utc_now,
) -> UndoResult:
    """Revert the latest applied turn not undone yet (see the module docstring); no Claude call.

    Raises:
        NothingToUndoError: no applied turn is left to undo.
        UndoConflictError: a file the turn changed was changed again afterwards; nothing written.
    """
    target = await asyncio.to_thread(_undo_target, vault, subject_slug, topic_slug)
    if target is None or target.commit is None:
        raise NothingToUndoError("No hay ningún cambio de la conversación que deshacer.")
    before = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug) or ""
    message = f"Deshecho en {subject_slug}/{topic_slug}: {_short(target.summary or '')}"
    try:
        commit = await asyncio.to_thread(sync.revert_paths, target.commit, target.paths, message)
    except RevertConflictError as error:
        raise UndoConflictError(
            "No se puede deshacer ese cambio: los apuntes (o las instrucciones) han cambiado"
            " después. Deshaz antes los cambios posteriores."
        ) from error
    after = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug) or ""
    changed = after != before
    result = UndoResult(
        subject=subject_slug,
        topic=topic_slug,
        undone_commit=target.commit,
        summary=target.summary,
        commit=commit,
        notes_changed=changed,
        diff=_diff(before, after) if changed else "",
        notes=after if changed else None,
        paths=target.paths,
        revision=notes_revision(after),
    )
    payload = result.model_dump(mode="json")
    await _Conversation(vault, subject_slug, topic_slug, clock).record(
        NOTES_UNDONE_KIND, detail=payload
    )
    sync.note_change()
    await _emit(on_event, NOTES_UNDONE_KIND, _event_payload(payload), subject_slug, topic_slug)
    return result


def _short(text: str, width: int = 72) -> str:
    words = " ".join(text.split())
    return words if len(words) <= width else words[:width].rstrip() + "…"


__all__ = [
    "EDIT_TOOL",
    "EXPLANATION_RECORD",
    "MAX_REASKS",
    "NOTES_EDITED_KIND",
    "NOTES_UNDONE_KIND",
    "REPLY_DELTA",
    "REPLY_RESTART",
    "STUDENT_EDIT_RECORD",
    "ChatHistory",
    "ChatRef",
    "ChatTurn",
    "EditsOutput",
    "InvalidMessageError",
    "NotesMissingError",
    "NothingToUndoError",
    "RevisionError",
    "RevisionResult",
    "UndoConflictError",
    "UndoResult",
    "chat_history",
    "revise_notes",
    "undo_last_revision",
]
