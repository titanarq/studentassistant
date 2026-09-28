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

**The student's selection** (#433): a typed workspace message carries what the student selected
in Recursos (`selected_sources`). The selected pages are sent after the topic's sources, just
before the message and marked as the current selection (`inputs.selection_input`: a captured
page's transcription and always its image, a PDF page's text), and their images take the image
budget first (`max_page_images`), so «reescribe esto con el texto de la captura» works on them.
An empty selection (a typed message with nothing selected) tells the editor so: it asks which
pages «esto» is instead of guessing. `None` (a spoken request, the old notes chat) says nothing.
With a selection, the facts of the selected captures and of those the notes cite follow it
(`overlap.capture_facts`), and a contradiction between a selected capture and another capture of
the same kind not selected is sent back (`overlap.same_kind_contradiction_errors`, #474).

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
first; a regeneration cannot be undone this way), and when the revert would change no file (a
commit that does not carry the turn's files): nothing is recorded then. A turn's write and its
commit are one locked step (`notes_lock.checkpointing`, #410), so a turn applied now always has a
commit to revert, unless the git lock stayed busy (`commit` None, said in its `warning`). Undoing
again goes one more turn back.

**Spoken requests**: a turn may come from a request the student said aloud (an `assistant.request`
of the session, run by the server's `assistant_requests.py`): `request` (`ChatRequestRef`) is then
the transcript span it came from -- its `request_id`, short `summary`, `session_id`,
`segment_ids`, times and raw `text`, which is the turn's message --, and the turn's `origin` is
`voice` (`typed` otherwise). Both are stored in the `revision` record and read back by
`chat_history` (`origin`, `request_summary`, `transcript`); records written before them read as
typed. `turn_id` is the id the caller gave the turn (the workspace stream's), stored too.

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
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.editor.doubts import (
    PENDING_REVIEWED_RECORD,
    EditorDoubt,
    LiveSink,
    SourceOption,
    doubt_chat_turns,
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
from studentassistant.editor.feedback import (
    NOT_RECORDED_WARNING,
    FeedbackRef,
    confirmation,
    excerpt,
    feedback_instruction,
    feedback_tool,
    has_feedback_call,
    parse_feedback,
    record_feedback,
)
from studentassistant.editor.inputs import (
    MAX_ATTACHMENT_BYTES,
    MAX_PAGE_IMAGES,
    DigestReader,
    EditorInput,
    SelectionInput,
    assemble_input,
    selection_input,
)
from studentassistant.editor.notes_format import (
    FidelityMode,
    notes_revision,
    topic_source_resolver,
    validate,
)
from studentassistant.editor.notes_lock import checkpointing, holding_notes
from studentassistant.editor.overlap import capture_facts, same_kind_contradiction_errors
from studentassistant.editor.reviewed import settled_blocks
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
from studentassistant.sources.triage import TriageReason
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
INCORPORATION_RECORD = "incorporation"
"""The conversation record of an incorporation of a few sources (`incorporate.py`, #326): a chat
turn of kind `incorporate`, undone like a revision turn."""
TRIAGE_RECORD = "triage"
"""The conversation record of captures set aside or restored from the chat (#327): a chat turn of
kind `triage` (`record_triage_turn`); it changes no notes, so nothing undoes it."""
MAX_REASKS = 2
"""How many times a change that fails the checks is sent back to the editor."""
NO_CALL_NOTE = (
    "El estudiante te ha pedido un cambio en los apuntes, pero tu respuesta no ha llamado a"
    f" `{EDIT_TOOL}`, así que los apuntes no han cambiado. Si hay que cambiarlos, escribe otra vez"
    f" una respuesta breve y llama a `{EDIT_TOOL}` con el cambio completo. Si no puedes o no"
    " debes cambiar nada, contesta sin llamarla y di claramente que los apuntes no han cambiado"
    " y por qué."
)
"""The one re-ask of an `edit` request answered without a tool call (#452)."""
NO_CHANGE_WARNING = "Los apuntes no han cambiado: el asistente no ha aplicado ningún cambio."
"""The warning of an `edit` request whose turn changed nothing, even after that re-ask (#452)."""
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
DOUBTS_NOT_RECORDED = (
    "El cambio está aplicado, pero no se han podido guardar las dudas que ha encontrado el editor."
)

NOT_UNDOABLE_WARNING = (
    "El cambio está aplicado, pero no se ha podido guardar como un paso propio en el historial:"
    " este cambio no se podrá deshacer."
)
"""An applied turn whose checkpoint returned no commit (the git lock stayed busy)."""


def _with_warning(current: str | None, extra: str) -> str:
    """`extra` after the turn's warning so far, if any."""
    return f"{current} {extra}" if current else extra


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
    doubts: list[EditorDoubt] = Field(
        default_factory=list,
        description="Every point this change leaves unresolved (an illegible word, sources that"
        " disagree, something missing): never written into the notes, asked in the chat.",
    )


TurnOrigin = Literal["typed", "voice"]
"""How the student asked: typed in the chat, or said aloud (an `assistant.request`)."""


class ChatRequestRef(_Strict):
    """The transcript span a spoken request came from (an `assistant.request` of a session)."""

    request_id: str = Field(description="`req-<n>`, the session's n-th request.")
    summary: str = Field(description="A short Spanish line of what was asked, for the chat.")
    session_id: str
    segment_ids: list[str] = Field(default_factory=list)
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=0)
    text: str = Field(description="The raw transcript of the span (the turn's message).")


class RevisionResult(_Strict):
    """What one turn of the conversation did; recorded as the `revision` conversation record."""

    subject: str
    topic: str
    turn_id: str | None = Field(default=None, description="The caller's id of the turn.")
    origin: TurnOrigin = "typed"
    request: ChatRequestRef | None = Field(
        default=None, description="The spoken request the turn answers (`origin` `voice`)."
    )
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
    doubts: list[str] = Field(
        default_factory=list, description="The pending ids of the doubts the turn raised."
    )
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
    feedback: FeedbackRef | None = Field(
        default=None,
        description="The app feedback item the turn recorded (`report_feedback`, #472).",
    )


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


class TriageTarget(_Strict):
    """One target of a `set_aside` from the chat and why it is set aside (#351).

    `reasons` are the capture's triage reasons (`sources.triage.TriageReason`: `blank`,
    `duplicate`, `blurry`, `partial`, `same_content`) -- empty when the student set aside a page
    triage found nothing wrong with; `duplicate_of` is the page it repeats (topic-relative) for
    `duplicate` / `same_content`. `already` when it was set aside before the request.
    """

    source_id: str
    reasons: list[TriageReason] = Field(default_factory=list)
    duplicate_of: str | None = None
    already: bool = False


class ChatTurn(_Strict):
    """One turn of the conversation as the web chat shows it.

    `kind` is `revise` for a revision turn and `explain` for a "¿Por qué pusiste esto?" answer,
    whose `refs` are the sources of the block it explains; `student_edit`, a save of the student's
    own edit of the document (its `summary` holds the diff), is only in the editor's history.

    `origin` is `voice` for a turn that answers a spoken request: `request_summary` is then the
    short line of what was asked and `transcript` the span it came from (both `None` when typed).
    `summary` stays the applied change's summary.

    `incorporate` is an incorporation of a few sources (`incorporate.py`, #326): `source_ids`
    (what was incorporated), `summary`, `reply`, `diff` and `commit`; undone like a `revise` turn.

    `triage` is captures set aside or restored from the chat (#327): `source_ids`, `summary`
    (`set_aside` or `restore`), the short `reply` («He apartado la página 3.») and, for a
    `set_aside`, `targets` (each target with its triage reasons, #351; empty for a restore and
    for records written before).

    `doubt` is a doubt the chat asked (#325): `pending_id`, `question` (also the `reply`),
    `suggestions`, `options`, `refs` (the sources it is about), `status` (`open` until answered)
    and, once closed, `resolution` and `answer`. `doubts_resolved` is the short line reporting
    the doubts the editor settled from the sources itself (its `reply`), with their ids in
    `pending_ids`.
    """

    time: datetime
    kind: Literal[
        "revise", "explain", "student_edit", "doubt", "doubts_resolved", "incorporate", "triage"
    ] = "revise"
    turn_id: str | None = None
    origin: TurnOrigin = "typed"
    request_summary: str | None = None
    transcript: ChatRequestRef | None = None
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
    pending_id: str | None = None
    question: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    options: list[SourceOption] = Field(default_factory=list)
    doubt_refs: list[str] = Field(
        default_factory=list, description="`doubt`: the sources the doubt is about."
    )
    status: str | None = Field(
        default=None, description="`doubt`: open, resolved, auto_resolved or dismissed."
    )
    resolution: str | None = None
    answer: str | None = Field(default=None, description="`doubt`: what the student answered.")
    pending_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(
        default_factory=list, description="`incorporate`: the sources incorporated."
    )
    diff: str = Field(default="", description="`incorporate`: unified diff of `apuntes.md`.")
    targets: list[TriageTarget] = Field(
        default_factory=list, description="`triage` (`set_aside`): each target and its reasons."
    )
    feedback: FeedbackRef | None = Field(
        default=None, description="`revise`: the app feedback item the turn recorded (#472)."
    )


class _ExplanationView(BaseModel):
    """What the chat reads of an `explanation` record."""

    question: str
    reply: str
    refs: list[ChatRef] = Field(default_factory=list)
    warning: str | None = None


class _IncorporationView(BaseModel):
    """What the chat and the undo read of an `incorporation` record (`IncorporationResult`)."""

    turn_id: str | None = None
    origin: TurnOrigin = "typed"
    request: ChatRequestRef | None = None
    source_ids: list[str] = Field(default_factory=list)
    message: str = ""
    reply: str = ""
    applied: bool = False
    summary: str | None = None
    changed_sections: list[str] = Field(default_factory=list)
    diff: str = ""
    paths: list[str] = Field(default_factory=list)
    commit: str | None = None
    warning: str | None = None


class TriageTurn(_Strict):
    """Captures set aside or restored from the chat (#327); the `triage` conversation record."""

    turn_id: str | None = None
    origin: TurnOrigin = "typed"
    request: ChatRequestRef | None = Field(
        default=None, description="The spoken request it answers (`origin` `voice`)."
    )
    decision: Literal["set_aside", "restore"]
    source_ids: list[str] = Field(default_factory=list)
    message: str = Field(description="What was asked, as the chat shows it.")
    reply: str = Field(description="The short Spanish line of what was done.")
    applied: bool = Field(default=True, description="False when nothing changed.")
    targets: list[TriageTarget] = Field(
        default_factory=list,
        description="`set_aside`: every target (set aside now or before) and its triage reasons;"
        " empty for a `restore` and for records written before #351.",
    )


def record_triage_turn(
    vault: Vault, subject_slug: str, topic_slug: str, turn: TriageTurn, *, clock: Clock = _utc_now
) -> None:
    """Record `turn` in `conversations/editor.jsonl` so the chat shows it (blocking).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, SecretRefused, OSError: the vault's errors.
    """
    append_conversation_record(
        vault,
        subject_slug,
        topic_slug,
        CONVERSATION_NAME,
        ConversationRecord(time=clock(), kind=TRIAGE_RECORD, detail=turn.model_dump(mode="json")),
    )


class _UndoTarget(BaseModel):
    """The turn an undo reverts: a revision or an incorporation."""

    commit: str
    paths: list[str]
    summary: str | None = None


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
        if record.kind == PENDING_REVIEWED_RECORD and record.detail:
            line = record.detail.get("summary")
            ids = record.detail.get("auto_resolved")
            if isinstance(line, str) and line and isinstance(ids, list) and ids:
                turns.append(
                    ChatTurn(
                        time=record.time,
                        kind="doubts_resolved",
                        message="",
                        reply=line,
                        pending_ids=[str(i) for i in ids],
                    )
                )
            continue
        if record.kind == TRIAGE_RECORD and record.detail:
            try:
                triage = TriageTurn.model_validate(record.detail)
            except ValidationError:
                logger.warning(
                    "ignoring a malformed triage record of %s/%s", subject_slug, topic_slug
                )
                continue
            turns.append(
                ChatTurn(
                    time=record.time,
                    kind="triage",
                    turn_id=triage.turn_id,
                    origin=triage.origin,
                    request_summary=None if triage.request is None else triage.request.summary,
                    transcript=triage.request,
                    message=triage.message,
                    reply=triage.reply,
                    applied=triage.applied,
                    summary=triage.decision,
                    source_ids=triage.source_ids,
                    targets=triage.targets,
                )
            )
            continue
        if record.kind == INCORPORATION_RECORD and record.detail:
            try:
                view = _IncorporationView.model_validate(record.detail)
            except ValidationError:
                logger.warning(
                    "ignoring a malformed incorporation record of %s/%s", subject_slug, topic_slug
                )
                continue
            turns.append(
                ChatTurn(
                    time=record.time,
                    kind="incorporate",
                    turn_id=view.turn_id,
                    origin=view.origin,
                    request_summary=None if view.request is None else view.request.summary,
                    transcript=view.request,
                    message=view.message,
                    reply=view.reply,
                    applied=view.applied,
                    summary=view.summary,
                    changed_sections=view.changed_sections,
                    commit=view.commit,
                    warning=view.warning,
                    source_ids=view.source_ids,
                    diff=view.diff,
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
                turn_id=result.turn_id,
                origin=result.origin,
                request_summary=None if result.request is None else result.request.summary,
                transcript=result.request,
                message=result.message,
                reply=result.reply,
                applied=result.applied,
                summary=result.summary,
                changed_sections=result.changed_sections,
                commit=result.commit,
                warning=result.warning,
                proposed_style_rules=new_rules(guide, result.proposed_style_rules),
                feedback=result.feedback,
            )
        )
    return [
        turn.model_copy(update={"undone": turn.commit is not None and turn.commit in undone})
        for turn in turns
    ]


def _undo_target(vault: Vault, subject_slug: str, topic_slug: str) -> _UndoTarget | None:
    """The latest applied, committed turn (a revision or an incorporation) not undone yet."""
    undone: set[str] = set()
    candidates: list[_UndoTarget] = []
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION_NAME):
        if record.kind == NOTES_UNDONE_KIND and record.detail:
            commit = record.detail.get("undone_commit")
            if isinstance(commit, str):
                undone.add(commit)
        elif record.kind in (REVISION_RECORD, INCORPORATION_RECORD) and record.detail:
            model = RevisionResult if record.kind == REVISION_RECORD else _IncorporationView
            try:
                result = model.model_validate(record.detail)
            except ValidationError:
                continue
            if result.applied and result.commit and result.paths:
                candidates.append(
                    _UndoTarget(commit=result.commit, paths=result.paths, summary=result.summary)
                )
    remaining = [result for result in candidates if result.commit not in undone]
    return remaining[-1] if remaining else None


def chat_history(vault: Vault, subject_slug: str, topic_slug: str) -> ChatHistory:
    """The topic's revision turns, oldest first; reads only (blocking).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
    """
    turns = _read_turns(vault, subject_slug, topic_slug)
    doubts = [
        ChatTurn(
            time=turn.time,
            kind="doubt",
            message="",
            reply=turn.question,
            pending_id=turn.pending_id,
            question=turn.question,
            suggestions=turn.suggestions,
            options=turn.options,
            doubt_refs=turn.refs,
            status=turn.status,
            resolution=turn.resolution,
            answer=turn.answer,
            applied=turn.notes_changed,
        )
        for turn in doubt_chat_turns(vault, subject_slug, topic_slug)
    ]
    if doubts:
        turns = sorted([*turns, *doubts], key=lambda turn: turn.time)
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
        if turn.kind == "doubts_resolved":
            lines.append(f"[{turn.reply}]")
            lines.append("")
            continue
        said = " (en voz alta, transcrito)" if turn.origin == "voice" else ""
        if turn.kind == "incorporate":
            sources = ", ".join(turn.source_ids) or "(ninguna)"
            lines.append(f"[Incorporación de fuentes: {sources}]")
        if turn.kind == "triage":
            lines.append(f"Estudiante{said}: {turn.message}")
            lines.append(f"[{turn.reply}]")
            lines.append("")
            continue
        lines.append(f"Estudiante{said}: {turn.message}")
        reply = turn.reply or "(sin respuesta)"
        lines.append(f"Editor: {reply}")
        if turn.applied:
            state = " (el estudiante lo deshizo)" if turn.undone else ""
            lines.append(f"[Cambio aplicado: {turn.summary or 'sin resumen'}{state}]")
        elif turn.warning and turn.kind in ("revise", "incorporate"):
            lines.append("[No se aplicó ningún cambio: no pasó la validación.]")
        if turn.feedback is not None:
            lines.append(f"[Apuntado como comentario sobre la aplicación: {turn.feedback.title}]")
        if turn.proposed_style_rules:
            rules = "; ".join(f"«{rule}»" for rule in turn.proposed_style_rules)
            lines.append(f"[Propuesto para la guía de estilo, sin confirmar aún: {rules}]")
        lines.append("")
    return "\n".join(lines).rstrip()


NO_SELECTION_NOTE = (
    "(El estudiante no tiene ninguna fuente seleccionada en Recursos. Si su mensaje habla de"
    " «esto», «esta captura» o «estas páginas» sin nombrarlas, no adivines cuáles son: pregúntale"
    " de qué páginas habla; puede seleccionarlas en Recursos o decirte su número.)"
)
SELECTION_NOTE = (
    "(El estudiante tiene seleccionadas en Recursos las fuentes de «Selección actual del"
    " estudiante», más arriba: «esto» y «estas páginas» son esas.)"
)

SPOKEN_NOTE = (
    "(El estudiante lo ha dicho en voz alta mientras estudiaba: es la transcripción literal de lo"
    " que dijo, con sus posibles errores de reconocimiento.)"
)


def _turn_text(
    turns: list[ChatTurn],
    notes: str,
    mode: str,
    message: str,
    *,
    spoken: bool = False,
    selected: Sequence[str] | None = None,
    settled: Collection[str] = (),
) -> str:
    heading = "## Nuevo mensaje del estudiante\n\n" + (f"{SPOKEN_NOTE}\n\n" if spoken else "")
    if selected is not None:
        heading += f"{SELECTION_NOTE if selected else NO_SELECTION_NOTE}\n\n"
    return (
        "## Conversación hasta ahora\n\n"
        f"{_history_text(turns)}\n\n"
        f"## Mapa de bloques de los apuntes actuales (modo de fidelidad «{mode}»)\n\n"
        f"{describe_sections(notes, settled)}\n\n"
        f"{heading}"
        f"{message}\n"
    )


def _stale_turn(
    response: LLMResponse, notes: str, mode: str, settled: Collection[str] = ()
) -> dict[str, Any]:
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
            f" «{mode}»)\n\n{describe_sections(notes, settled)}\n\nRespeta lo que ha escrito el"
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
    selected: Sequence[str] = (),
) -> tuple[list[str], str | None]:
    """`(errors, edited notes)` of a change; with a Recursos `selection`, no contradiction
    between a selected capture and another of the same kind not selected (#474)."""
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
    errors.extend(editor_doubt_errors(value.doubts, assembled))
    if selected:
        errors.extend(same_kind_contradiction_errors(value.doubts, selected))
    try:
        edited = apply_edits(notes, value.ops, value.footnotes)
    except EditError as error:
        return [*errors, *error.errors], None
    mode: FidelityMode = value.fidelity_mode or assembled.fidelity_mode
    resolver = topic_source_resolver(vault, assembled.subject_slug, assembled.topic_slug)
    errors.extend(validate(edited, mode, resolver, editor_written=True, previous=notes))
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
    """Write and commit one change as one locked step; blocking. `(paths, commit, new mode, rules
    added)`.

    The writes and their checkpoint run under the vault's git lock (`checkpointing`), so the sync
    loop cannot commit these files first and leave the turn's commit -- the one an undo reverts
    -- without them.
    """
    root = vault.path
    paths: list[str] = []
    with checkpointing(sync) as commit_now:
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
        commit = commit_now(
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
    request: ChatRequestRef | None = None,
    turn_id: str | None = None,
    live: LiveSink | None = None,
    host: str | None = None,
    selected_sources: Sequence[str] | None = None,
    expects_change: bool = False,
    session_id: str | None = None,
) -> RevisionResult:
    """One turn of the revision conversation (see the module docstring).

    `expects_change` is set for a request classified `edit` (#452): an answer without an
    `apply_edits` call is then re-asked once (`NO_CALL_NOTE`), and a turn that still calls no
    tool carries `NO_CHANGE_WARNING`, so a reply that only *says* it changed the notes never
    passes silently. A `question` (and the old notes chat) keeps chat-only answers as they are.

    The editor may instead record app feedback with `report_feedback` (#472, `feedback.py`): the
    item goes to the vault's feedback inbox with the turn's context (`session_id`, or the
    spoken request's session), the result's `feedback` names it, and such a turn is never
    re-asked for `apply_edits` nor flagged `NO_CHANGE_WARNING`.

    `selected_sources` is the student's Recursos selection of a typed message (#433),
    topic-relative ids in order (a PDF page as `<pdf>#page=K`): sent first within the image
    budget and marked; `[]` tells the editor nothing is selected; `None` says nothing.

    `request` is the spoken request the turn answers (`message` is then its raw `text`): the turn
    is stored with `origin` `voice` and that reference. `turn_id` is stored as given.

    The `doubts` the applied change reports become pending items with their questions
    (`doubts.raise_doubts`: in the topic's live session through `live` when it has an unended
    one, else a review session); their ids are the result's `doubts`. A failure to record them
    never loses the applied change: it is logged and the result's `warning` says so.

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
    selection: SelectionInput | None = None
    if selected_sources:
        selection = await asyncio.to_thread(
            partial(
                selection_input,
                vault,
                subject_slug,
                topic_slug,
                list(selected_sources),
                max_page_images=max_page_images,
                max_attachment_bytes=max_attachment_bytes,
            )
        )
    assembled: EditorInput = await asyncio.to_thread(
        assemble_input,
        vault,
        subject_slug,
        topic_slug,
        prompt=prompt,
        digest=digest,
        max_page_images=max_page_images - (len(selection.images) if selection else 0),
        max_attachment_bytes=max_attachment_bytes
        - (selection.attachment_bytes if selection else 0),
        instruction=REVISE_INSTRUCTION + feedback_instruction(),
    )
    notes = _seeded(base, assembled.topic_title)
    settled = await asyncio.to_thread(settled_blocks, vault, subject_slug, topic_slug, base)
    turn = {
        "type": "text",
        "text": _turn_text(
            turns,
            notes,
            assembled.fidelity_mode,
            text,
            spoken=request is not None,
            selected=None if selected_sources is None else list(selected_sources),
            settled=settled,
        ),
    }
    # The selection goes after the cached prefix (the topic's sources), just before the turn,
    # with the facts of the selected captures and those the notes cite (#474).
    facts = (
        await asyncio.to_thread(
            capture_facts, vault, subject_slug, topic_slug, list(selected_sources), base
        )
        if selected_sources
        else ""
    )
    facts_block = [{"type": "text", "text": facts}] if facts else []
    chosen = [*(selection.content if selection else []), *facts_block]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [*assembled.content, *chosen, turn]}
    ]
    tool = strict_tool(
        EDIT_TOOL,
        "Apply this turn's changes to the notes: edit ops, new footnotes, a summary, and the"
        " standing instructions the student gave.",
        EditsOutput,
    )
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)
    feedback: FeedbackRef | None = None
    feedback_failed = False

    model = client.model
    value: EditsOutput | None = None
    edited: str | None = None
    applied: _Applied | None = None
    stale = False
    nudged = False
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
            tools=[tool, feedback_tool()],
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
                detail={
                    "reason": "revise",
                    **assembled.summary(),
                    **(
                        {
                            "selected_sources": list(selected_sources or ()),
                            "selected_images": selection.images if selection else [],
                        }
                        if selected_sources is not None
                        else {}
                    ),
                },
            )
            chosen_record = [*(selection.record_content if selection else []), *facts_block]
            await conversation.record(
                "user",
                message={
                    "role": "user",
                    "content": [*assembled.record_content, *chosen_record, turn],
                },
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
        if feedback is None and has_feedback_call(response):
            feedback = await _record_feedback(
                vault,
                response,
                subject_slug,
                topic_slug,
                session_id=session_id or (request.session_id if request is not None else None),
                chat_excerpt=excerpt(_excerpt_lines(turns), text),
                sync=sync,
                clock=clock,
            )
            feedback_failed = feedback is None
        # A feedback turn never changes the notes, even if the editor also called `apply_edits`.
        value, errors = (None, []) if has_feedback_call(response) else _parse_call(response)
        stale = False
        if value is not None:
            errors, edited = await asyncio.to_thread(
                _check,
                value,
                notes,
                assembled,
                vault,
                _pending_rules(turns),
                list(selected_sources or ()),
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
        no_call = (
            expects_change and value is None and not errors and not has_feedback_call(response)
        )
        if no_call and not nudged and attempt <= MAX_REASKS:
            nudged = True
            await conversation.record(
                "validation", detail={"attempt": attempt, "errors": [NO_CALL_NOTE]}
            )
            nudge = {"role": "user", "content": [{"type": "text", "text": NO_CALL_NOTE}]}
            messages = [*messages, response.assistant_turn(), nudge]
            await conversation.record("user", message=nudge, model=model)
            continue
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

    result = RevisionResult(
        subject=subject_slug,
        topic=topic_slug,
        turn_id=turn_id,
        origin="typed" if request is None else "voice",
        request=request,
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
    elif expects_change and value is None and feedback is None and not feedback_failed:
        result = result.model_copy(update={"warning": NO_CHANGE_WARNING})
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
                "warning": NOT_UNDOABLE_WARNING if paths and commit is None else None,
            }
        )
    if feedback is not None:
        result = result.model_copy(
            update={"feedback": feedback, "reply": result.reply or confirmation(feedback)}
        )
    elif feedback_failed:
        result = result.model_copy(
            update={"warning": _with_warning(result.warning, NOT_RECORDED_WARNING)}
        )
    if value is not None and not errors and value.doubts:
        try:
            raised = await raise_doubts(
                vault, subject_slug, topic_slug, value.doubts, sync=sync, host=host, live=live
            )
        except Exception:
            logger.exception(
                "could not record the doubts of a turn of %s/%s", subject_slug, topic_slug
            )
            result = result.model_copy(
                update={"warning": _with_warning(result.warning, DOUBTS_NOT_RECORDED)}
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
    await conversation.record(REVISION_RECORD, model=model, prompt_hash=prompt.hash, detail=payload)
    sync.note_change()  # the conversation's last lines, committed by the sync loop
    if result.applied:
        await _emit(on_event, NOTES_EDITED_KIND, _event_payload(payload), subject_slug, topic_slug)
    return result


def _excerpt_lines(turns: list[ChatTurn]) -> list[str]:
    """The last two chat turns, one line each side: the context kept with app feedback."""
    lines: list[str] = []
    for turn in [t for t in turns if t.kind != "student_edit"][-2:]:
        if turn.message:
            lines.append(f"Estudiante: {' '.join(turn.message.split())[:200]}")
        if turn.reply:
            lines.append(f"Editor: {' '.join(turn.reply.split())[:200]}")
    return lines


async def _record_feedback(
    vault: Vault,
    response: LLMResponse,
    subject_slug: str,
    topic_slug: str,
    *,
    session_id: str | None,
    chat_excerpt: str,
    sync: GitSync,
    clock: Clock,
) -> FeedbackRef | None:
    report = parse_feedback(response)
    if report is None:
        return None
    return await record_feedback(
        vault,
        report,
        subject_slug=subject_slug,
        topic_slug=topic_slug,
        mode="construir",
        route="workspace",
        session_id=session_id,
        chat_excerpt=chat_excerpt,
        sync=sync,
        clock=clock,
    )


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
        UndoConflictError: a file the turn changed was changed again afterwards, or reverting the
            turn's commit would change no file (it does not carry them); nothing written and no
            `notes.undone` recorded.
    """
    target = await asyncio.to_thread(_undo_target, vault, subject_slug, topic_slug)
    if target is None:
        raise NothingToUndoError("No hay ningún cambio de la conversación que deshacer.")
    before = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug) or ""
    files_before = await asyncio.to_thread(_file_contents, vault, target.paths)
    message = f"Deshecho en {subject_slug}/{topic_slug}: {_short(target.summary or '')}"
    try:
        commit = await asyncio.to_thread(sync.revert_paths, target.commit, target.paths, message)
    except RevertConflictError as error:
        raise UndoConflictError(
            "No se puede deshacer ese cambio: los apuntes (o las instrucciones) han cambiado"
            " después. Deshaz antes los cambios posteriores."
        ) from error
    if commit is None and await asyncio.to_thread(_file_contents, vault, target.paths) == (
        files_before
    ):
        # The turn's commit does not carry its files (the sync loop committed them first, in a
        # turn applied before #410): the revert changed no file, so no undo is recorded. (A
        # revert written but not committed -- git failing -- is still recorded, `commit` None.)
        raise UndoConflictError(
            "No se puede deshacer ese cambio: no quedó guardado como un paso propio en el"
            " historial, así que deshacerlo no cambiaría nada. Los apuntes siguen igual."
        )
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


def _file_contents(vault: Vault, paths: Sequence[str]) -> list[bytes | None]:
    """The bytes of each vault-relative path (`None` when absent); reads only (blocking)."""
    contents: list[bytes | None] = []
    for path in paths:
        file = vault.path / path
        contents.append(file.read_bytes() if file.is_file() else None)
    return contents


def _short(text: str, width: int = 72) -> str:
    words = " ".join(text.split())
    return words if len(words) <= width else words[:width].rstrip() + "…"


__all__ = [
    "EDIT_TOOL",
    "EXPLANATION_RECORD",
    "INCORPORATION_RECORD",
    "MAX_REASKS",
    "NOTES_EDITED_KIND",
    "NOTES_UNDONE_KIND",
    "NOT_UNDOABLE_WARNING",
    "REPLY_DELTA",
    "REPLY_RESTART",
    "STUDENT_EDIT_RECORD",
    "TRIAGE_RECORD",
    "ChatHistory",
    "ChatRef",
    "ChatRequestRef",
    "ChatTurn",
    "EditsOutput",
    "InvalidMessageError",
    "NotesMissingError",
    "NothingToUndoError",
    "RevisionError",
    "RevisionResult",
    "TriageTarget",
    "TriageTurn",
    "TurnOrigin",
    "UndoConflictError",
    "UndoResult",
    "chat_history",
    "record_triage_turn",
    "revise_notes",
    "undo_last_revision",
]
