"""Doubts resolution: the editor settles what the sources answer and asks the student the rest.

After the notes are written, the observer's pending doubts (`observer.pending`) are worked
through with the `editor` role (Opus) in two steps:

- **Review** (`review_doubts`, tool `resolve_doubts`): one decision per open doubt. The editor
  *auto-resolves* a doubt only with evidence -- at least one source of the topic's catalogue (or a
  transcript span of one of its sessions) and what it says -- and gives the edit ops that apply the
  resolution to the notes; otherwise it *asks*: a Spanish question with 1-3 suggested answers, or,
  for a contradiction, the conflicting sources as options for the student to pick. The decisions
  are checked (every open doubt once, evidence cited from the catalogue, 1-3 suggestions, options
  for a contradiction, edits that apply and notes that still pass the provenance validator) and
  sent back with the errors at most `MAX_REASKS` times; past that nothing is auto-resolved and every
  doubt becomes a question, the notes untouched.
- **Answer** (`answer_doubt`, tool `apply_decision`): the student answers one question -- a
  suggestion, their own words, or the source that is right in a contradiction, optionally keeping
  a note with the discarded version ("En tus apuntes pone 1791") -- and the editor gives the edit
  ops that make the notes follow the decision (validated and re-asked the same way). Past the
  re-asks the decision is still recorded and the notes left as they were, with a warning; without
  notes yet no call is made (the next generation follows the decision).
- **Dismiss** (`dismiss_doubt`): the student discards the doubt; no call, no edit.

**Where the decisions go.** Every close is event-sourced (ADR-0003): an `observer.state_op`
`resolve_pending` event (status `auto_resolved` from the `editor`, `resolved`/`dismissed` from the
`user`), which is what closes the item in the observer's fold, followed by a `pending.resolved`
event with the details (`DoubtOutcome`); each question is a `pending.question` event
(`DoubtQuestion`). They are written to a **review session** of the topic -- a session started and
ended at once for them (`vault.start_session(..., kind="review")` / `end_session`), with no
transcript -- so that they fold after every study session before it; its `kind` keeps it out of
the student's study sessions (#191). While the topic has an unended session nothing is
written (`OpenSessionError`): its later events would fold before the review's. Then
`review/pending.yaml` is regenerated (`load_observer_snapshot`) and the notes (when edited) and the
events are committed together with a Spanish summary. The calls are recorded in
`conversations/editor.jsonl` like the generation's.

`list_doubts` is the queue the web panel shows: every item with its latest question and outcome,
the open ones first, and `current`, the one to ask next (one at a time).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
from studentassistant.editor.notes_format import topic_source_resolver, validate
from studentassistant.llm import LLMClient, LLMResponse, StructuredResult, load_prompt, structured
from studentassistant.observer import (
    STATE_OP_EVENT_KIND,
    EventRef,
    PendingItem,
    ResolvePending,
    TopicState,
    load_observer_snapshot,
    op_payload,
)
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Origin,
    Vault,
    append_conversation_record,
    end_session,
    list_sessions,
    read_notes,
    read_topic_events,
    start_session,
    write_notes,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "editor_doubts"
CONVERSATION_NAME = "editor"
REVIEW_TOOL = "resolve_doubts"
DECISION_TOOL = "apply_decision"
PENDING_QUESTION_KIND = "pending.question"
PENDING_RESOLVED_KIND = "pending.resolved"
MAX_REASKS = 2
"""How many times a review or a decision that fails the checks is sent back to the editor."""
MAX_SUGGESTIONS = 3

Clock = Callable[[], datetime]
_TRANSCRIPT_ID = re.compile(
    r"^sessions/(?P<session>\d{8}-\d{6})#t=\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}$"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- errors --------------------------------------------------------------------------------------


class DoubtError(ValueError):
    """A doubts request that cannot be carried out; the message is Spanish, for the student."""


class UnknownDoubtError(DoubtError, LookupError):
    """The topic has no pending item with that id."""


class DoubtClosedError(DoubtError):
    """The item was already resolved, auto-resolved or dismissed."""


class InvalidAnswerError(DoubtError):
    """The answer does not fit the doubt's question (no suggestion of that number, ...)."""


class OpenSessionError(DoubtError):
    """The topic has an unended session: its events would fold before the review's."""


class NotesMissingError(DoubtError):
    """The review needs the notes: the topic has no `notes/apuntes.md` yet."""


# -- what the editor answers ---------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """A source that settles a doubt: its catalogue id (or transcript span) and what it says."""

    source_id: str = Field(
        description="As the catalogue names it, or sessions/<id>#t=HH:MM:SS-HH:MM:SS."
    )
    quote: str = Field(description="What the source says, briefly.")


class SourceOption(_Strict):
    """One side of a contradiction: a source and what it says."""

    source_id: str
    says: str


class DoubtDecision(_Strict):
    """The editor's decision on one open doubt (the `resolve_doubts` tool)."""

    pending_id: str
    action: Literal["auto_resolve", "ask"]
    resolution: str | None = Field(default=None, description="auto_resolve: the answer, Spanish.")
    evidence: list[Evidence] = Field(default_factory=list)
    edits: list[EditOp] = Field(default_factory=list)
    question: str | None = Field(default=None, description="ask: the question, Spanish.")
    suggestions: list[str] = Field(default_factory=list, description="ask: 1-3 likely answers.")
    options: list[SourceOption] = Field(
        default_factory=list, description="ask, contradiction: one per conflicting source."
    )


class DoubtsReviewOutput(_Strict):
    """The input of the `resolve_doubts` tool."""

    decisions: list[DoubtDecision]
    footnotes: list[NewFootnote] = Field(
        default_factory=list, description="Footnote definitions the auto-resolution edits add."
    )


class DecisionOutput(_Strict):
    """The input of the `apply_decision` tool."""

    resolution: str = Field(description="What was decided, one Spanish sentence.")
    edits: list[EditOp] = Field(default_factory=list)
    footnotes: list[NewFootnote] = Field(default_factory=list)


# -- what is recorded and served ------------------------------------------------------------------


class DoubtQuestion(_Strict):
    """A `pending.question` payload: what the student is asked about one doubt."""

    pending_id: str
    question: str
    suggestions: list[str] = Field(default_factory=list)
    options: list[SourceOption] = Field(default_factory=list)
    asked_at: EventRef | None = None


class DoubtOutcome(_Strict):
    """A `pending.resolved` payload: how one doubt was closed, and what it did to the notes."""

    pending_id: str
    status: Literal["resolved", "auto_resolved", "dismissed"]
    resolution: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    answer: str | None = Field(default=None, description="The student's own words.")
    suggestion: str | None = Field(default=None, description="The suggestion the student picked.")
    chosen_source: SourceOption | None = None
    discarded: list[SourceOption] = Field(default_factory=list)
    keep_discarded: bool = False
    notes_changed: bool = False
    warning: str | None = None
    resolved_at: EventRef | None = None


class Doubt(_Strict):
    """One item of the queue: the observer's item, its latest question and its outcome."""

    item: PendingItem
    question: DoubtQuestion | None = None
    outcome: DoubtOutcome | None = None


class DoubtsQueue(_Strict):
    """What the web's pending panel shows: `current` is the open doubt to ask next."""

    subject: str
    topic: str
    open_count: int
    current: str | None = None
    items: list[Doubt] = Field(default_factory=list)


class DoubtAnswer(_Strict):
    """The student's answer: a suggestion (by number, from 1), free text, or a source.

    `source_id` picks the source that is right in a contradiction (one of the question's
    `options`); `keep_discarded` then asks for a note keeping what the other sources say. `answer`
    may go with either as a comment.
    """

    suggestion: int | None = Field(default=None, ge=1, le=MAX_SUGGESTIONS)
    answer: str | None = Field(default=None, max_length=2000)
    source_id: str | None = None
    keep_discarded: bool = False


class ReviewResult(_Strict):
    """What one review did."""

    subject: str
    topic: str
    auto_resolved: list[str] = Field(default_factory=list)
    asked: list[str] = Field(default_factory=list)
    notes_changed: bool = False
    session_id: str | None = Field(default=None, description="The review session written.")
    commit: str | None = None
    attempts: int = 0
    warning: str | None = None
    model: str | None = None


class ResolutionResult(_Strict):
    """What answering or dismissing one doubt did."""

    subject: str
    topic: str
    pending_id: str
    status: Literal["resolved", "dismissed"]
    resolution: str | None = None
    notes_changed: bool = False
    session_id: str | None = None
    commit: str | None = None
    attempts: int = 0
    warning: str | None = None
    model: str | None = None


# -- reading the queue ----------------------------------------------------------------------------


def _event_ref(session_id: str, seq: int) -> EventRef:
    return EventRef(session_id=session_id, seq=seq)


def _read_records(
    vault: Vault, subject_slug: str, topic_slug: str
) -> tuple[dict[str, DoubtQuestion], dict[str, DoubtOutcome]]:
    """The latest question and outcome of each pending id, from the topic's events."""
    questions: dict[str, DoubtQuestion] = {}
    outcomes: dict[str, DoubtOutcome] = {}
    for session_id, event in read_topic_events(vault, subject_slug, topic_slug):
        if event.kind not in (PENDING_QUESTION_KIND, PENDING_RESOLVED_KIND):
            continue
        at = _event_ref(session_id, event.seq)
        try:
            if event.kind == PENDING_QUESTION_KIND:
                question = DoubtQuestion.model_validate({**event.payload, "asked_at": at})
                questions[question.pending_id] = question
            else:
                outcome = DoubtOutcome.model_validate({**event.payload, "resolved_at": at})
                outcomes[outcome.pending_id] = outcome
        except ValueError:
            logger.warning("ignoring a malformed %s event (%s, seq %s)", event.kind, *at.key())
    return questions, outcomes


def _latest(records: dict[str, Any], item: PendingItem) -> Any:
    for pending_id in (item.id, *reversed(item.merged_ids)):
        if pending_id in records:
            return records[pending_id]
    return None


def _state(vault: Vault, subject_slug: str, topic_slug: str) -> TopicState:
    return load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False).state


def list_doubts(vault: Vault, subject_slug: str, topic_slug: str) -> DoubtsQueue:
    """The topic's doubts queue; reads only (blocking: async callers use a worker thread).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
        ObserverStateError: the topic's events cannot be folded.
    """
    state = _state(vault, subject_slug, topic_slug)
    questions, outcomes = _read_records(vault, subject_slug, topic_slug)
    open_items = state.open_pending()
    items = [
        Doubt(item=item, question=_latest(questions, item), outcome=_latest(outcomes, item))
        for item in [*open_items, *state.resolved_pending()]
    ]
    return DoubtsQueue(
        subject=subject_slug,
        topic=topic_slug,
        open_count=len(open_items),
        current=open_items[0].id if open_items else None,
        items=items,
    )


# -- writing ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Event:
    kind: str
    origin: Origin
    payload: dict[str, Any]


def _close_events(item: PendingItem, outcome: DoubtOutcome, origin: Origin) -> list[_Event]:
    op = ResolvePending(pending_id=item.id, resolution=outcome.resolution, status=outcome.status)
    return [
        _Event(STATE_OP_EVENT_KIND, origin, op_payload(op)),
        _Event(
            PENDING_RESOLVED_KIND,
            origin,
            outcome.model_dump(mode="json", exclude={"resolved_at"}, exclude_none=True),
        ),
    ]


def _require_no_open_session(vault: Vault, subject_slug: str, topic_slug: str) -> None:
    if any(meta.ended_at is None for meta in list_sessions(vault, subject_slug, topic_slug)):
        raise OpenSessionError(
            "Este tema tiene una sesión sin terminar: termínala antes de resolver sus dudas."
        )


def _write(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    *,
    host: str,
    events: Sequence[_Event],
    notes: str | None,
    message: str,
) -> tuple[str | None, str | None]:
    """Write the notes and a review session holding `events`, then commit; blocking.

    Returns the review session id (None without events) and the commit.
    """
    _require_no_open_session(vault, subject_slug, topic_slug)
    if notes is not None:
        write_notes(vault, subject_slug, topic_slug, notes)
    session_id = None
    if events:
        session = start_session(
            vault, subject_slug, topic_slug, host, PROTOCOL_VERSION, kind="review"
        )
        session_id = session.id
        try:
            for event in events:
                session.append_event(event.kind, event.origin, event.payload)
        finally:
            end_session(session)
        # Regenerates `review/pending.yaml` and the snapshot from the fold that now closes them.
        load_observer_snapshot(vault, subject_slug, topic_slug)
    sync.note_change()
    return session_id, sync.checkpoint(message)


# -- the editor calls ---------------------------------------------------------------------------


class _Conversation:
    """Appends the calls of one task to `conversations/editor.jsonl`; a lost line is only logged."""

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


def _reask_turn(response: LLMResponse, tool_name: str, errors: list[str]) -> dict[str, Any]:
    listing = "\n".join(f"- {error}" for error in errors)
    reason = f"La respuesta no se puede aplicar por estos motivos:\n\n{listing}"
    content: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": reason}
        for call in response.tool_calls
    ]
    content.append(
        {
            "type": "text",
            "text": f"{reason}\n\nCorrígelos y llama otra vez a `{tool_name}` con la respuesta"
            " completa.",
        }
    )
    return {"role": "user", "content": content}


@dataclass
class _Task:
    """One editor task over the assembled topic: the call loop with checks and re-asks."""

    client: LLMClient
    assembled: EditorInput
    conversation: _Conversation
    prompt_hash: str
    reason: str
    confirm_over_cap: bool

    async def run[T: BaseModel](
        self,
        output: type[T],
        tool_name: str,
        tool_description: str,
        check: Callable[[T], list[str]],
    ) -> tuple[T | None, int, str, list[str]]:
        """`(value, attempts, model, errors)`: the first value that passes `check`, else None."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.assembled.content}]
        model = self.client.model
        errors: list[str] = []
        attempt = 0
        for attempt in range(1, MAX_REASKS + 2):
            result: StructuredResult[T] = await structured(
                self.client,
                messages,
                output,
                tool_name=tool_name,
                tool_description=tool_description,
                system=self.assembled.system,
                prompt_hash=self.prompt_hash,
                confirm_over_cap=self.confirm_over_cap,
            )
            if attempt == 1:
                await self.conversation.record(
                    "context",
                    model=self.client.model,
                    prompt_hash=self.prompt_hash,
                    detail={"reason": self.reason, **self.assembled.summary()},
                )
                await self.conversation.record(
                    "user",
                    message={"role": "user", "content": self.assembled.record_content},
                    model=self.client.model,
                )
            for response in result.responses:
                model = response.model or model
                await self.conversation.record(
                    "assistant",
                    message=response.assistant_turn(),
                    model=model,
                    prompt_hash=self.prompt_hash,
                    usage=response.usage.model_dump(),
                )
            errors = await asyncio.to_thread(check, result.value)
            await self.conversation.record(
                "validation", detail={"attempt": attempt, "errors": errors}
            )
            if not errors:
                return result.value, attempt, model, []
            if attempt > MAX_REASKS:
                break
            last = result.responses[-1]
            reask = _reask_turn(last, tool_name, errors)
            messages = [*messages, last.assistant_turn(), reask]
            await self.conversation.record("user", message=reask, model=model)
        return None, attempt, model, errors


def _citable(assembled: EditorInput, source_id: str) -> bool:
    if source_id in {source.source_id for source in assembled.catalogue}:
        return True
    transcript = _TRANSCRIPT_ID.match(source_id)
    return transcript is not None and transcript["session"] in assembled.sessions


def _item_line(item: PendingItem) -> str:
    return f"- {item.id} [{item.kind}] {item.text}"


async def _assemble(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    instruction: str,
    digest: DigestReader | None,
    max_page_images: int,
    max_attachment_bytes: int,
) -> tuple[EditorInput, str]:
    prompt = load_prompt(PROMPT_NAME)
    assembled = await asyncio.to_thread(
        assemble_input,
        vault,
        subject_slug,
        topic_slug,
        prompt=prompt,
        digest=digest,
        max_page_images=max_page_images,
        max_attachment_bytes=max_attachment_bytes,
        instruction=instruction,
    )
    return assembled, prompt.hash


def _notes_errors(assembled: EditorInput, vault: Vault, text: str) -> list[str]:
    resolver = topic_source_resolver(vault, assembled.subject_slug, assembled.topic_slug)
    return validate(text, assembled.fidelity_mode, resolver)


# -- review ---------------------------------------------------------------------------------------


def _review_instruction(items: list[PendingItem], notes: str) -> str:
    listing = "\n".join(_item_line(item) for item in items)
    return (
        "## Tarea: revisar las dudas abiertas (herramienta `resolve_doubts`)\n\n"
        "En esta tarea sí resuelves las dudas abiertas que tus fuentes contestan, citándolas, y"
        " preguntas al estudiante las demás. Da una decisión por cada una de estas dudas:\n\n"
        f"{listing}\n\n"
        "Mapa de bloques de los apuntes actuales (para las ediciones):\n\n"
        f"{describe_sections(notes)}\n"
    )


def _check_review(
    value: DoubtsReviewOutput,
    items: list[PendingItem],
    assembled: EditorInput,
    vault: Vault,
    notes: str,
) -> list[str]:
    errors: list[str] = []
    by_id = {item.id: item for item in items}
    seen: set[str] = set()
    edits: list[EditOp] = []
    for decision in value.decisions:
        where = f"Duda {decision.pending_id}"
        item = by_id.get(decision.pending_id)
        if item is None:
            errors.append(f"{where}: no es ninguna de las dudas abiertas.")
            continue
        if decision.pending_id in seen:
            errors.append(f"{where}: tiene más de una decisión.")
            continue
        seen.add(decision.pending_id)
        if decision.action == "auto_resolve":
            if not (decision.resolution and decision.resolution.strip()):
                errors.append(f"{where}: auto_resolve necesita la resolución.")
            if not decision.evidence:
                errors.append(
                    f"{where}: auto_resolve necesita al menos una fuente que lo demuestre; si no"
                    " la hay, pregunta al estudiante (ask)."
                )
            for evidence in decision.evidence:
                if not _citable(assembled, evidence.source_id):
                    errors.append(
                        f"{where}: la fuente {evidence.source_id} no está en el catálogo ni es un"
                        " fragmento de una sesión del tema."
                    )
                if not evidence.quote.strip():
                    errors.append(f"{where}: di qué dice la fuente {evidence.source_id}.")
            edits.extend(decision.edits)
        else:
            if not (decision.question and decision.question.strip()):
                errors.append(f"{where}: ask necesita la pregunta para el estudiante.")
            if decision.edits:
                errors.append(
                    f"{where}: una pregunta no lleva ediciones (se aplican al responder)."
                )
            suggestions = [s for s in decision.suggestions if s.strip()]
            if len(suggestions) > MAX_SUGGESTIONS:
                errors.append(f"{where}: da como mucho {MAX_SUGGESTIONS} respuestas sugeridas.")
            if item.kind == "contradiction":
                if len(decision.options) < 2:
                    errors.append(
                        f"{where}: en una contradicción da una opción por cada fuente en conflicto"
                        " (al menos dos)."
                    )
                for option in decision.options:
                    if not _citable(assembled, option.source_id):
                        errors.append(
                            f"{where}: la fuente {option.source_id} no está en el catálogo ni es"
                            " un fragmento de una sesión del tema."
                        )
            elif not suggestions:
                errors.append(f"{where}: da de 1 a {MAX_SUGGESTIONS} respuestas sugeridas.")
    for item in items:
        if item.id not in seen:
            errors.append(f"Duda {item.id}: falta su decisión.")
    if errors:
        return errors
    try:
        edited = apply_edits(notes, edits, value.footnotes)
    except EditError as error:
        return error.errors
    return _notes_errors(assembled, vault, edited)


def _fallback_question(item: PendingItem, decision: DoubtDecision | None) -> DoubtQuestion:
    """The question a doubt gets when the review could not be checked."""
    if decision is not None and decision.action == "ask" and decision.question:
        suggestions = [s for s in decision.suggestions if s.strip()][:MAX_SUGGESTIONS]
        return DoubtQuestion(
            pending_id=item.id,
            question=decision.question,
            suggestions=suggestions,
            options=decision.options,
        )
    suggestions = [decision.resolution] if decision and decision.resolution else []
    return DoubtQuestion(
        pending_id=item.id,
        question=f"¿Cómo resolvemos esta duda? {item.text}",
        suggestions=suggestions,
    )


async def review_doubts(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    client: LLMClient,
    sync: GitSync,
    host: str | None = None,
    digest: DigestReader | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> ReviewResult:
    """Review every open doubt of the topic with the editor (see the module docstring).

    Nothing is sent when no doubt is open.

    Raises:
        NotesMissingError: the topic has no notes yet.
        OpenSessionError: the topic has an unended session.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing written.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    await asyncio.to_thread(_require_no_open_session, vault, subject_slug, topic_slug)
    items = (await asyncio.to_thread(_state, vault, subject_slug, topic_slug)).open_pending()
    if not items:
        return ReviewResult(subject=subject_slug, topic=topic_slug)
    notes = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    if not notes or not notes.strip():
        raise NotesMissingError(
            "Todavía no hay apuntes de este tema: prepáralos antes de revisar las dudas."
        )
    assembled, prompt_hash = await _assemble(
        vault,
        subject_slug,
        topic_slug,
        _review_instruction(items, notes),
        digest,
        max_page_images,
        max_attachment_bytes,
    )
    task = _Task(
        client,
        assembled,
        _Conversation(vault, subject_slug, topic_slug, clock),
        prompt_hash,
        "doubts_review",
        confirm_over_cap,
    )
    last: list[DoubtsReviewOutput] = []

    def check(value: DoubtsReviewOutput) -> list[str]:
        last[:] = [value]
        return _check_review(value, items, assembled, vault, notes)

    value, attempts, model, _errors = await task.run(
        DoubtsReviewOutput,
        REVIEW_TOOL,
        "Record one decision per open doubt: auto-resolve it with cited evidence and edits, or"
        " ask the student.",
        check,
    )

    events: list[_Event] = []
    auto: list[str] = []
    asked: list[str] = []
    new_notes: str | None = None
    warning = None
    by_id = {decision.pending_id: decision for decision in (last[0].decisions if last else [])}
    if value is not None:
        edits = [e for d in value.decisions if d.action == "auto_resolve" for e in d.edits]
        edited = apply_edits(notes, edits, value.footnotes)
        new_notes = edited if edited != notes else None
    for item in items:
        decision = by_id.get(item.id)
        if value is not None and decision is not None and decision.action == "auto_resolve":
            outcome = DoubtOutcome(
                pending_id=item.id,
                status="auto_resolved",
                resolution=decision.resolution,
                evidence=decision.evidence,
                notes_changed=bool(decision.edits) and new_notes is not None,
            )
            events.extend(_close_events(item, outcome, "editor"))
            auto.append(item.id)
            continue
        if value is not None and decision is not None:
            question = DoubtQuestion(
                pending_id=item.id,
                question=decision.question or item.text,
                suggestions=[s for s in decision.suggestions if s.strip()],
                options=decision.options,
            )
        else:
            question = _fallback_question(item, decision)
        events.append(
            _Event(
                PENDING_QUESTION_KIND,
                "editor",
                question.model_dump(mode="json", exclude={"asked_at"}),
            )
        )
        asked.append(item.id)
    if value is None:
        warning = (
            "El editor no ha podido resolver las dudas citando tus fuentes; quedan todas como"
            " preguntas y los apuntes no han cambiado."
        )
    message = (
        f"Dudas de {subject_slug}/{topic_slug} revisadas: {len(auto)} resueltas con fuentes,"
        f" {len(asked)} preguntas"
    )
    session_id, commit = await asyncio.to_thread(
        _write,
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=events,
        notes=new_notes,
        message=message,
    )
    result = ReviewResult(
        subject=subject_slug,
        topic=topic_slug,
        auto_resolved=auto,
        asked=asked,
        notes_changed=new_notes is not None,
        session_id=session_id,
        commit=commit,
        attempts=attempts,
        warning=warning,
        model=model,
    )
    await task.conversation.record(
        "pending.reviewed",
        model=model,
        prompt_hash=prompt_hash,
        detail=result.model_dump(mode="json"),
    )
    sync.note_change()  # the conversation's last line, committed by the sync loop
    return result


# -- answer and dismiss -------------------------------------------------------------------------


def _open_item(vault: Vault, subject_slug: str, topic_slug: str, pending_id: str) -> PendingItem:
    item = _state(vault, subject_slug, topic_slug).pending_item(pending_id)
    if item is None:
        raise UnknownDoubtError("No existe esa duda en este tema.")
    if not item.is_open:
        raise DoubtClosedError("Esa duda ya está cerrada.")
    return item


def _decision_text(
    item: PendingItem, question: DoubtQuestion | None, answer: DoubtAnswer
) -> tuple[DoubtOutcome, str]:
    """The outcome (without resolution) and the Spanish text of the student's decision."""
    outcome = DoubtOutcome(
        pending_id=item.id, status="resolved", keep_discarded=answer.keep_discarded
    )
    parts: list[str] = []
    free = answer.answer.strip() if answer.answer and answer.answer.strip() else None
    if answer.suggestion is not None:
        suggestions = question.suggestions if question is not None else []
        if answer.suggestion > len(suggestions):
            raise InvalidAnswerError("Esa respuesta sugerida no existe para esta duda.")
        outcome.suggestion = suggestions[answer.suggestion - 1]
        parts.append(outcome.suggestion)
    if answer.source_id is not None:
        options = question.options if question is not None else []
        chosen = next((o for o in options if o.source_id == answer.source_id), None)
        if chosen is None:
            raise InvalidAnswerError("Esa fuente no es ninguna de las opciones de esta duda.")
        outcome.chosen_source = chosen
        outcome.discarded = [o for o in options if o.source_id != answer.source_id]
        parts.append(f"Vale lo que dice {chosen.source_id}: «{chosen.says}».")
    elif answer.keep_discarded:
        raise InvalidAnswerError(
            "Solo se puede conservar la versión descartada al elegir una fuente."
        )
    if free is not None:
        outcome.answer = free
        parts.append(free)
    if not parts:
        raise InvalidAnswerError("Elige una respuesta sugerida, una fuente o escribe tu respuesta.")
    return outcome, " ".join(parts)


def _answer_instruction(
    item: PendingItem,
    question: DoubtQuestion | None,
    outcome: DoubtOutcome,
    decision: str,
    notes: str,
) -> str:
    lines = [
        "## Tarea: aplicar la decisión del estudiante (herramienta `apply_decision`)",
        "",
        "Duda:",
        _item_line(item),
    ]
    if question is not None:
        lines.append(f"Pregunta: {question.question}")
    lines.append(f"Decisión del estudiante: {decision}")
    if outcome.discarded:
        others = "; ".join(f"{o.source_id} dice «{o.says}»" for o in outcome.discarded)
        lines.append(f"Fuentes descartadas: {others}.")
        if outcome.keep_discarded:
            lines.append(
                "Conserva la versión descartada: junto al texto corregido, añade una nota breve"
                " con lo que dice la otra fuente, citada a esa fuente (p. ej. «En tus apuntes pone"
                " 1791.»)."
            )
        else:
            lines.append("No conserves la versión descartada.")
    lines += [
        "",
        "Mapa de bloques de los apuntes actuales (para las ediciones):",
        "",
        describe_sections(notes),
    ]
    return "\n".join(lines) + "\n"


async def answer_doubt(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    pending_id: str,
    answer: DoubtAnswer,
    *,
    client: LLMClient,
    sync: GitSync,
    host: str | None = None,
    digest: DigestReader | None = None,
    confirm_over_cap: bool = False,
    clock: Clock = _utc_now,
    max_page_images: int = MAX_PAGE_IMAGES,
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
) -> ResolutionResult:
    """Record the student's answer to one doubt and apply it to the notes with the editor.

    Raises:
        UnknownDoubtError, DoubtClosedError, InvalidAnswerError, OpenSessionError: nothing sent.
        CostConfirmationRequiredError, RefusalError, LLMError: nothing written.
    """
    await asyncio.to_thread(_require_no_open_session, vault, subject_slug, topic_slug)
    item = await asyncio.to_thread(_open_item, vault, subject_slug, topic_slug, pending_id)
    questions, _ = await asyncio.to_thread(_read_records, vault, subject_slug, topic_slug)
    question = _latest(questions, item)
    outcome, decision = _decision_text(item, question, answer)
    notes = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)

    attempts, model, new_notes = 0, None, None
    resolution = decision
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)
    prompt_hash = None
    if notes and notes.strip():
        assembled, prompt_hash = await _assemble(
            vault,
            subject_slug,
            topic_slug,
            _answer_instruction(item, question, outcome, decision, notes),
            digest,
            max_page_images,
            max_attachment_bytes,
        )
        task = _Task(client, assembled, conversation, prompt_hash, "doubt_answer", confirm_over_cap)

        def check(value: DecisionOutput) -> list[str]:
            if not value.resolution.strip():
                return ["Falta la resolución."]
            try:
                edited = apply_edits(notes, value.edits, value.footnotes)
            except EditError as error:
                return error.errors
            return _notes_errors(assembled, vault, edited)

        value, attempts, model, _errors = await task.run(
            DecisionOutput,
            DECISION_TOOL,
            "Record the student's decision on the doubt and the edits that apply it to the notes.",
            check,
        )
        if value is None:
            outcome.warning = (
                "La decisión queda guardada, pero el editor no ha podido aplicarla a los apuntes"
                " cumpliendo las reglas de procedencia: revísalos o pídeselo en el chat."
            )
        else:
            resolution = value.resolution.strip()
            edited = apply_edits(notes, value.edits, value.footnotes)
            new_notes = edited if edited != notes else None
    outcome.resolution = resolution
    outcome.notes_changed = new_notes is not None
    session_id, commit = await asyncio.to_thread(
        _write,
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=_close_events(item, outcome, "user"),
        notes=new_notes,
        message=f"Duda resuelta en {subject_slug}/{topic_slug}: {_short(item.text)}",
    )
    result = ResolutionResult(
        subject=subject_slug,
        topic=topic_slug,
        pending_id=item.id,
        status="resolved",
        resolution=resolution,
        notes_changed=outcome.notes_changed,
        session_id=session_id,
        commit=commit,
        attempts=attempts,
        warning=outcome.warning,
        model=model,
    )
    if attempts:
        await conversation.record(
            PENDING_RESOLVED_KIND,
            model=model,
            prompt_hash=prompt_hash,
            detail=result.model_dump(mode="json"),
        )
        sync.note_change()
    return result


async def dismiss_doubt(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    pending_id: str,
    *,
    sync: GitSync,
    host: str | None = None,
) -> ResolutionResult:
    """Discard one doubt: closed as `dismissed`, no call, the notes untouched.

    Raises:
        UnknownDoubtError, DoubtClosedError, OpenSessionError: nothing written.
    """
    await asyncio.to_thread(_require_no_open_session, vault, subject_slug, topic_slug)
    item = await asyncio.to_thread(_open_item, vault, subject_slug, topic_slug, pending_id)
    outcome = DoubtOutcome(pending_id=item.id, status="dismissed")
    session_id, commit = await asyncio.to_thread(
        _write,
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=_close_events(item, outcome, "user"),
        notes=None,
        message=f"Duda descartada en {subject_slug}/{topic_slug}: {_short(item.text)}",
    )
    return ResolutionResult(
        subject=subject_slug,
        topic=topic_slug,
        pending_id=item.id,
        status="dismissed",
        session_id=session_id,
        commit=commit,
    )


def _short(text: str, width: int = 60) -> str:
    words = " ".join(text.split())
    return words if len(words) <= width else words[:width].rstrip() + "…"


__all__ = [
    "DECISION_TOOL",
    "MAX_REASKS",
    "PENDING_QUESTION_KIND",
    "PENDING_RESOLVED_KIND",
    "REVIEW_TOOL",
    "DecisionOutput",
    "Doubt",
    "DoubtAnswer",
    "DoubtClosedError",
    "DoubtDecision",
    "DoubtError",
    "DoubtOutcome",
    "DoubtQuestion",
    "DoubtsQueue",
    "DoubtsReviewOutput",
    "Evidence",
    "InvalidAnswerError",
    "NotesMissingError",
    "OpenSessionError",
    "ResolutionResult",
    "ReviewResult",
    "SourceOption",
    "UnknownDoubtError",
    "answer_doubt",
    "dismiss_doubt",
    "list_doubts",
    "review_doubts",
]
