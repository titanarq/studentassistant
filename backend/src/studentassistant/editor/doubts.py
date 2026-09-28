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

When the student answers or dismisses a doubt, the blocks then citing its sources are recorded as
reviewed (`reviewed.record_reviewed`, reason `doubt_closed`, #474); every block map the editor sees
here marks the settled ones «[revisado]» (`reviewed.settled_blocks`). The review's edits never
change or delete a settled block nor make one cite a source an open doubt names
(`overlap.settled_block_errors`, re-asked otherwise; #483), and its
instruction tells the editor to auto-resolve, with the block's source as evidence, a doubt about
what a settled block already says instead of asking it (`SETTLED_REVIEW_RULE`).

**Where the decisions go.** Every close is event-sourced (ADR-0003): an `observer.state_op`
`resolve_pending` event (status `auto_resolved` from the `editor`, `resolved`/`dismissed` from the
`user`), which is what closes the item in the observer's fold, followed by a `pending.resolved`
event with the details (`DoubtOutcome`); each question is a `pending.question` event
(`DoubtQuestion`). Without an unended session of the topic they are written to a **review
session** -- a session started and ended at once for them (`vault.start_session(...,
kind="review")` / `end_session`), with no transcript -- so that they fold after every study
session before it; its `kind` keeps it out of the student's study sessions (#191). With an
unended session (#325) they go to **that live session** through `live` (a `LiveSink`, the server's
publish on the session bus), since a review session would fold before its later events; without
a `live` that reaches it nothing is written (`OpenSessionError`). Then `review/pending.yaml` is
regenerated (`load_observer_snapshot`) and the events are committed with a Spanish summary. The
notes are edited on the latest version: under the topic's short write lock, only when they are
still those the editor's block map came from, else the editor is re-asked with the new block map
(a student save meanwhile), like a revision turn. The calls are recorded in
`conversations/editor.jsonl` like the generation's.

**Doubts never go into the notes** (#325): every write of the editor is validated with
`editor_written=True` (no `[[?...]]` mark in a block it changes), and what an editor write leaves
unresolved is reported as `EditorDoubt`s, recorded here as pending items with their questions
(`raise_doubts`). The workspace chat asks them **one at a time** (`ask_plan`, `ask_in_chat`: a
`pending.question` with `in_chat: true`), and `doubt_chat_turns` is what the chat shows of them.

`list_doubts` is the queue the web panel shows: every item with its latest question and outcome,
the open ones first, and `current`, the one to ask next (one at a time).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.edits import (
    SETTLED_MARK,
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
    capture_pages,
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
from studentassistant.editor.overlap import settled_block_errors
from studentassistant.editor.reviewed import (
    blocks_citing,
    item_sources,
    open_doubt_sources,
    record_reviewed,
    settled_blocks,
    source_key,
)
from studentassistant.llm import LLMClient, LLMResponse, StructuredResult, load_prompt, structured
from studentassistant.observer import (
    STATE_OP_EVENT_KIND,
    AddPending,
    EventRef,
    PendingItem,
    PendingKind,
    ResolvePending,
    TopicState,
    load_observer_snapshot,
    op_payload,
)
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.sources import set_aside_ids
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
PENDING_REVIEWED_RECORD = "pending.reviewed"
"""The conversation record of one review (`ReviewResult`)."""
MAX_REASKS = 2
"""How many times a review or a decision that fails the checks is sent back to the editor."""
MAX_SUGGESTIONS = 3
MAX_EDITOR_DOUBTS = 10
"""The most doubts one editor write may raise."""
NOTES_CHANGED_NOTE = "el estudiante ha cambiado los apuntes mientras respondías"

Clock = Callable[[], datetime]
LiveSink = Callable[[str, Origin, dict[str, Any]], Awaitable[str]]
"""Publishes one event in the topic's live session (unended and active on this backend) and
returns that session's id; raises when it cannot (no such session, or it ended meanwhile). The
server passes one that goes through its session bus, so the live observer folds the event too."""
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


class EditorDoubt(_Strict):
    """An unresolved point an editor write reports instead of writing it into the notes (#325).

    An illegible or uncertain word, a disagreement between sources (`contradiction`, with the
    sides as `options`), something missing: the notes carry the reading the sources support best
    (or leave the fragment out) and the doubt becomes a pending item asked in the chat.
    """

    kind: PendingKind = Field(
        description="illegible, unexplained_concept, incomplete, possible_error or contradiction."
    )
    text: str = Field(description="What the doubt is about, one Spanish sentence.")
    question: str = Field(description="The question for the student, Spanish, short.")
    suggestions: list[str] = Field(
        default_factory=list, description="1-3 likely answers, each short enough to be a button."
    )
    options: list[SourceOption] = Field(
        default_factory=list,
        description="contradiction: one per source in conflict (at least two), what each says.",
    )
    refs: list[str] = Field(
        default_factory=list,
        description="The sources it is about, as the catalogue names them (or a transcript span"
        " sessions/<id>#t=HH:MM:SS-HH:MM:SS).",
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
    in_chat: bool = Field(
        default=False,
        description="True when this is the question asked in the workspace chat (one at a time);"
        " a question recorded by a review or an editor write is not asked until then.",
    )
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
    summary: str | None = Field(
        default=None,
        description="One short Spanish line reporting the auto-resolved doubts (for the chat).",
    )
    revision: str | None = Field(default=None, description="The notes' revision after it.")
    session_id: str | None = Field(
        default=None, description="The session written: a review session, or the live one."
    )
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
    revision: str | None = Field(default=None, description="The notes' revision after it.")
    session_id: str | None = None
    commit: str | None = None
    attempts: int = 0
    warning: str | None = None
    model: str | None = None


# -- reading the queue ----------------------------------------------------------------------------


def _event_ref(session_id: str, seq: int) -> EventRef:
    return EventRef(session_id=session_id, seq=seq)


@dataclass
class _Records:
    """The doubts records of a topic's events, in log order."""

    questions: dict[str, DoubtQuestion] = field(default_factory=dict)
    """The latest question of each pending id."""
    outcomes: dict[str, DoubtOutcome] = field(default_factory=dict)
    """The latest outcome of each pending id."""
    chat: list[DoubtQuestion] = field(default_factory=list)
    """Every question asked in the chat, in order."""


def _read_all(vault: Vault, subject_slug: str, topic_slug: str) -> _Records:
    records = _Records()
    for session_id, event in read_topic_events(vault, subject_slug, topic_slug):
        if event.kind not in (PENDING_QUESTION_KIND, PENDING_RESOLVED_KIND):
            continue
        at = _event_ref(session_id, event.seq)
        try:
            if event.kind == PENDING_QUESTION_KIND:
                question = DoubtQuestion.model_validate({**event.payload, "asked_at": at})
                records.questions[question.pending_id] = question
                if question.in_chat:
                    records.chat.append(question)
            else:
                outcome = DoubtOutcome.model_validate({**event.payload, "resolved_at": at})
                records.outcomes[outcome.pending_id] = outcome
        except ValueError:
            logger.warning("ignoring a malformed %s event (%s, seq %s)", event.kind, *at.key())
    return records


def _read_records(
    vault: Vault, subject_slug: str, topic_slug: str
) -> tuple[dict[str, DoubtQuestion], dict[str, DoubtOutcome]]:
    """The latest question and outcome of each pending id, from the topic's events."""
    records = _read_all(vault, subject_slug, topic_slug)
    return records.questions, records.outcomes


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


def open_doubt(vault: Vault, subject_slug: str, topic_slug: str, pending_id: str) -> Doubt:
    """One open doubt with its latest question (`None` when it has none yet); reads only
    (blocking).

    Raises:
        UnknownDoubtError, DoubtClosedError: no such doubt, or it is closed.
    """
    item = _open_item(vault, subject_slug, topic_slug, pending_id)
    questions, _ = _read_records(vault, subject_slug, topic_slug)
    return Doubt(item=item, question=_latest(questions, item), outcome=None)


# -- which doubt the chat asks next ----------------------------------------------------------------


def _cited_keys(notes: str | None) -> set[str]:
    keys: set[str] = set()
    for definition in parse(notes or "").footnotes:
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            continue
        if provenance.source_id is not None:
            keys.add(source_key(provenance.source_id))
    return keys


def _item_page_keys(item: PendingItem, pages: dict[str, str]) -> set[str]:
    """The source files an item is about: its captures' pages and its source refs."""
    keys = {source_key(pages[capture]) for capture in item.refs.pages if capture in pages}
    keys |= {source_key(ref) for ref in item.refs.sources}
    return {key for key in keys if not key.startswith("sessions/")}


def _item_refs(item: PendingItem, pages: dict[str, str]) -> list[str]:
    """What the chat shows an item is about: its pages' and sources' ids, in order."""
    refs = [pages[capture] for capture in item.refs.pages if capture in pages]
    refs += list(item.refs.sources)
    return list(dict.fromkeys(refs))


@dataclass(frozen=True)
class AskPlan:
    """What the chat does next about the topic's doubts (`ask_plan`)."""

    asked: str | None
    """An open doubt asked in the chat and not answered yet: nothing else is asked meanwhile."""
    to_review: list[str]
    """Open doubts relevant to the notes with no question yet (queue order): review them first."""
    to_ask: list[str]
    """Open doubts relevant to the notes with a question, not asked in the chat (queue order)."""


def ask_plan(vault: Vault, subject_slug: str, topic_slug: str) -> AskPlan:
    """Which open doubt the workspace chat asks next, one at a time; reads only (blocking).

    Only open doubts relevant to the current notes count -- their refs overlap the sources the
    notes cite (a transcript segment counts as its session), or they have no refs --, in the
    queue's order (`list_doubts`' `current` first). An item whose pages and sources are all set
    aside by capture triage (#324) is never asked. Without notes nothing is asked.
    """
    notes = read_notes(vault, subject_slug, topic_slug)
    if not notes or not notes.strip():
        return AskPlan(asked=None, to_review=[], to_ask=[])
    state = _state(vault, subject_slug, topic_slug)
    records = _read_all(vault, subject_slug, topic_slug)
    pages = capture_pages(vault, subject_slug, topic_slug)
    set_aside = set_aside_ids(vault, subject_slug, topic_slug)
    cited = _cited_keys(notes)
    in_chat = {question.pending_id for question in records.chat}
    asked: str | None = None
    to_review: list[str] = []
    to_ask: list[str] = []
    for item in state.open_pending():
        page_keys = _item_page_keys(item, pages)
        if page_keys and all(key in set_aside for key in page_keys):
            continue
        if any(pending_id in in_chat for pending_id in (item.id, *item.merged_ids)):
            asked = asked or item.id
            continue
        keys = page_keys | {
            f"sessions/{state.segments[segment].session_id}"
            for segment in item.refs.segments
            if segment in state.segments
        }
        if keys and not keys & cited:
            continue
        if _latest(records.questions, item) is None:
            to_review.append(item.id)
        else:
            to_ask.append(item.id)
    return AskPlan(asked=asked, to_review=to_review, to_ask=to_ask)


class DoubtChatTurn(_Strict):
    """One doubt asked in the chat, with how it ended (`chat_history` turns of kind `doubt`)."""

    time: datetime
    pending_id: str
    kind: str
    text: str = Field(default="", description="What the doubt is about (its explanation).")
    question: str
    suggestions: list[str] = Field(default_factory=list)
    options: list[SourceOption] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "auto_resolved", "dismissed"] = "open"
    resolution: str | None = None
    answer: str | None = Field(default=None, description="What the student answered.")
    notes_changed: bool = False
    resolved_time: datetime | None = None


def doubt_chat_turns(vault: Vault, subject_slug: str, topic_slug: str) -> list[DoubtChatTurn]:
    """The doubts asked in the chat, oldest first, each with its outcome; reads only (blocking).

    Built from the `pending.question` events asked in the chat and the `pending.resolved` ones. A
    doubt shown in the chat again (#516: a badge of the notes clicked) is one turn, at its latest
    asking.
    """
    records = _read_all(vault, subject_slug, topic_slug)
    if not records.chat:
        return []
    state = _state(vault, subject_slug, topic_slug)
    latest: dict[str, DoubtQuestion] = {}
    for question in records.chat:
        item = state.pending_item(question.pending_id)
        key = item.id if item is not None else question.pending_id
        latest.pop(key, None)
        latest[key] = question
    pages = capture_pages(vault, subject_slug, topic_slug)
    started = {meta.id: meta.started_at for meta in list_sessions(vault, subject_slug, topic_slug)}

    def time_of(ref: EventRef | None, t: int | None) -> datetime | None:
        if ref is None or ref.session_id not in started:
            return None
        return started[ref.session_id] + timedelta(milliseconds=t or 0)

    times = _event_times(vault, subject_slug, topic_slug)
    turns: list[DoubtChatTurn] = []
    for question in latest.values():
        item = state.pending_item(question.pending_id)
        asked = time_of(question.asked_at, times.get(_key(question.asked_at)))
        if item is None or asked is None:
            continue
        outcome = _latest(records.outcomes, item) if not item.is_open else None
        answer = None
        if outcome is not None:
            parts = [outcome.suggestion, outcome.chosen_source and outcome.chosen_source.says]
            parts.append(outcome.answer)
            answer = " ".join(p for p in parts if p) or None
        turns.append(
            DoubtChatTurn(
                time=asked,
                pending_id=item.id,
                kind=item.kind,
                text=item.text,
                question=question.question,
                suggestions=question.suggestions,
                options=question.options,
                refs=_item_refs(item, pages),
                status=item.status,
                resolution=item.resolution,
                answer=answer,
                notes_changed=bool(outcome and outcome.notes_changed),
                resolved_time=None
                if outcome is None
                else time_of(outcome.resolved_at, times.get(_key(outcome.resolved_at))),
            )
        )
    return turns


def _key(ref: EventRef | None) -> tuple[str, int] | None:
    return None if ref is None else ref.key()


def _event_times(vault: Vault, subject_slug: str, topic_slug: str) -> dict[Any, int]:
    return {
        (session_id, event.seq): event.t
        for session_id, event in read_topic_events(vault, subject_slug, topic_slug)
        if event.kind in (PENDING_QUESTION_KIND, PENDING_RESOLVED_KIND)
    }


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


def _question_event(question: DoubtQuestion) -> _Event:
    return _Event(
        PENDING_QUESTION_KIND, "editor", question.model_dump(mode="json", exclude={"asked_at"})
    )


OPEN_SESSION_MESSAGE = (
    "Este tema tiene una sesión sin terminar: termínala antes de resolver sus dudas."
)


def _unended(vault: Vault, subject_slug: str, topic_slug: str) -> bool:
    return any(meta.ended_at is None for meta in list_sessions(vault, subject_slug, topic_slug))


def _require_writable(
    vault: Vault, subject_slug: str, topic_slug: str, live: LiveSink | None
) -> None:
    """An unended session takes the events only through `live`; without it, `OpenSessionError`."""
    if live is None and _unended(vault, subject_slug, topic_slug):
        raise OpenSessionError(OPEN_SESSION_MESSAGE)


def _review_session(
    vault: Vault, subject_slug: str, topic_slug: str, host: str, events: Sequence[_Event]
) -> str:
    session = start_session(vault, subject_slug, topic_slug, host, PROTOCOL_VERSION, kind="review")
    try:
        for event in events:
            session.append_event(event.kind, event.origin, event.payload)
    finally:
        end_session(session)
    return session.id


async def _write_events(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    host: str,
    events: Sequence[_Event],
    live: LiveSink | None,
) -> str:
    """Write `events` in the topic's live session when it has one, else in a review session.

    A live session's events fold after everything before them (ADR-0003), so the doubts of an
    unended session go there, through `live`; without an unended session they go to a review
    session started and ended at once for them, which folds after every session before it.

    Raises:
        OpenSessionError: the topic has an unended session and `live` cannot write to it.
    """
    if await asyncio.to_thread(_unended, vault, subject_slug, topic_slug):
        if live is None:
            raise OpenSessionError(OPEN_SESSION_MESSAGE)
        first, *rest = events
        try:
            session_id = await live(first.kind, first.origin, dict(first.payload))
        except Exception as error:
            if await asyncio.to_thread(_unended, vault, subject_slug, topic_slug):
                raise OpenSessionError(OPEN_SESSION_MESSAGE) from error
            # The session ended meanwhile: a review session folds after it again.
        else:
            for event in rest:
                await live(event.kind, event.origin, dict(event.payload))
            return session_id
    return await asyncio.to_thread(_review_session, vault, subject_slug, topic_slug, host, events)


async def _commit_events(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    *,
    host: str,
    events: Sequence[_Event],
    message: str,
    live: LiveSink | None = None,
) -> tuple[str | None, str | None]:
    """Write `events` (`_write_events`), regenerate `review/pending.yaml`, commit.

    Returns the session written (None without events) and the commit.
    """
    session_id = None
    if events:
        session_id = await _write_events(
            vault, subject_slug, topic_slug, host=host, events=events, live=live
        )

    def finish() -> str | None:
        if events:
            # Regenerates `review/pending.yaml` and the snapshot from the fold that now has them.
            load_observer_snapshot(vault, subject_slug, topic_slug)
        sync.note_change()
        return sync.checkpoint(message)

    return session_id, await asyncio.to_thread(finish)


def _write_notes_if_current(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    base: str | None,
    edited: str,
    message: str,
) -> tuple[str | None, str | None]:
    """Write and commit `edited` under the topic's write lock when the notes are still `base`.

    Blocking. The write and its checkpoint (`message`) are one locked step (`checkpointing`), so
    the sync loop never commits the doubt's edit under a batch subject. Returns `(None, commit)`
    when written, else `(the notes stored now, None)` (nothing written).
    """
    with holding_notes(vault, subject_slug, topic_slug):
        current = read_notes(vault, subject_slug, topic_slug)
        if current != base:
            return (current if current is not None else ""), None
        with checkpointing(sync) as commit_now:
            write_notes(vault, subject_slug, topic_slug, edited)
            return None, commit_now(message)


def _stored_revision(vault: Vault, subject_slug: str, topic_slug: str) -> str | None:
    stored = read_notes(vault, subject_slug, topic_slug)
    return None if stored is None else notes_revision(stored)


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


def _stale_turn(
    response: LLMResponse, tool_name: str, notes: str, settled: Collection[str] = ()
) -> dict[str, Any]:
    """The re-ask after the notes changed under the task: the new block map, answer again."""
    reason = (
        f"No se ha aplicado: {NOTES_CHANGED_NOTE}, así que los números de bloque ya no"
        " corresponden."
    )
    content: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": reason}
        for call in response.tool_calls
    ]
    content.append(
        {
            "type": "text",
            "text": f"{reason}\n\nMapa de bloques de los apuntes actuales (para las"
            f" ediciones):\n\n{describe_sections(notes, settled)}\n\nRespeta lo que ha escrito el"
            f" estudiante y llama otra vez a `{tool_name}` con la respuesta completa sobre estos"
            " apuntes.",
        }
    )
    return {"role": "user", "content": content}


Apply = Callable[[Any], Awaitable[str | None]]
"""Applies a checked value; returns None when done, else the notes stored now (they changed
since the task's block map was built, so nothing was applied and the editor is re-asked)."""


@dataclass
class _Task:
    """One editor task over the assembled topic: the call loop with checks and re-asks."""

    client: LLMClient
    assembled: EditorInput
    conversation: _Conversation
    prompt_hash: str
    reason: str
    confirm_over_cap: bool
    stale: bool = False
    """Whether the last attempt found the notes changed under it."""

    async def _settled(self, notes: str) -> set[str]:
        conversation = self.conversation
        return await asyncio.to_thread(
            settled_blocks, conversation.vault, conversation.subject, conversation.topic, notes
        )

    async def run[T: BaseModel](
        self,
        output: type[T],
        tool_name: str,
        tool_description: str,
        check: Callable[[T], list[str]],
        apply: Apply | None = None,
    ) -> tuple[T | None, int, str, list[str]]:
        """`(value, attempts, model, errors)`: the first value that passes `check` (and that
        `apply` applied), else None."""
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
            current: str | None = None
            self.stale = False
            if not errors and apply is not None:
                current = await apply(result.value)
                if current is not None:
                    self.stale = True
                    errors = [f"No se ha aplicado: {NOTES_CHANGED_NOTE}."]
            await self.conversation.record(
                "validation", detail={"attempt": attempt, "errors": errors}
            )
            if not errors:
                return result.value, attempt, model, []
            if attempt > MAX_REASKS:
                break
            last = result.responses[-1]
            reask = (
                _stale_turn(last, tool_name, current, await self._settled(current))
                if current is not None
                else _reask_turn(last, tool_name, errors)
            )
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
    return await _assemble_with(
        PROMPT_NAME,
        vault,
        subject_slug,
        topic_slug,
        instruction,
        digest,
        max_page_images,
        max_attachment_bytes,
    )


async def _assemble_with(
    prompt_name: str,
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    instruction: str,
    digest: DigestReader | None,
    max_page_images: int,
    max_attachment_bytes: int,
) -> tuple[EditorInput, str]:
    """The topic assembled with the `prompt_name` prompt and a task instruction, and its hash."""
    prompt = load_prompt(prompt_name)
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


def _notes_errors(
    assembled: EditorInput, vault: Vault, text: str, previous: str | None = None
) -> list[str]:
    """The validator's errors of notes the editor wrote (no doubt marks in what it changed)."""
    resolver = topic_source_resolver(vault, assembled.subject_slug, assembled.topic_slug)
    return validate(text, assembled.fidelity_mode, resolver, editor_written=True, previous=previous)


# -- doubts raised by an editor write -------------------------------------------------------------


def editor_doubt_errors(doubts: Sequence[EditorDoubt], assembled: EditorInput) -> list[str]:
    """Spanish errors of the `doubts` an editor write reports, for re-asking it."""
    errors: list[str] = []
    if len(doubts) > MAX_EDITOR_DOUBTS:
        errors.append(f"Da como mucho {MAX_EDITOR_DOUBTS} dudas en `doubts`.")
    for number, doubt in enumerate(doubts, start=1):
        where = f"Duda {number} de `doubts`"
        if not doubt.text.strip():
            errors.append(f"{where}: falta el texto (`text`) de la duda.")
        if not doubt.question.strip():
            errors.append(f"{where}: falta la pregunta (`question`) para el estudiante.")
        suggestions = [s for s in doubt.suggestions if s.strip()]
        if len(suggestions) > MAX_SUGGESTIONS:
            errors.append(f"{where}: da como mucho {MAX_SUGGESTIONS} respuestas sugeridas.")
        if doubt.kind == "contradiction":
            if len({option.source_id for option in doubt.options}) < 2:
                errors.append(
                    f"{where}: en una contradicción da una opción por cada fuente en conflicto"
                    " (al menos dos fuentes distintas), con lo que dice cada una."
                )
        elif not suggestions:
            errors.append(f"{where}: da de 1 a {MAX_SUGGESTIONS} respuestas sugeridas.")
        for option in doubt.options:
            if not _citable(assembled, option.source_id):
                errors.append(
                    f"{where}: la fuente {option.source_id} no está en el catálogo ni es un"
                    " fragmento de una sesión del tema."
                )
            if not option.says.strip():
                errors.append(f"{where}: di qué dice la fuente {option.source_id}.")
        for ref in doubt.refs:
            if not _citable(assembled, ref):
                errors.append(
                    f"{where}: {ref} no está en el catálogo ni es un fragmento de una sesión del"
                    " tema."
                )
    return errors


def _doubt_events(doubts: Sequence[EditorDoubt]) -> tuple[list[_Event], list[str]]:
    events: list[_Event] = []
    ids: list[str] = []
    for doubt in doubts:
        prefix = "contradiccion" if doubt.kind == "contradiction" else "duda"
        pending_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        refs = list(dict.fromkeys([*doubt.refs, *(option.source_id for option in doubt.options)]))
        op = AddPending(
            pending_id=pending_id, kind=doubt.kind, text=doubt.text.strip(), source_refs=refs
        )
        events.append(_Event(STATE_OP_EVENT_KIND, "editor", op_payload(op)))
        question = DoubtQuestion(
            pending_id=pending_id,
            question=doubt.question.strip(),
            suggestions=[s.strip() for s in doubt.suggestions if s.strip()][:MAX_SUGGESTIONS],
            options=doubt.options,
        )
        events.append(_question_event(question))
        ids.append(pending_id)
    return events, ids


async def raise_doubts(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    doubts: Sequence[EditorDoubt],
    *,
    sync: GitSync,
    host: str | None = None,
    live: LiveSink | None = None,
) -> list[str]:
    """Record the doubts an editor write reported as pending items with their questions.

    Each is an `observer.state_op` `add_pending` (origin `editor`; the fold merges one that
    duplicates an open item into it) followed by a `pending.question` (not asked in the chat yet:
    the chat asks one at a time). They go to the topic's live session through `live` when it has
    an unended one, else to a review session; then one commit. Returns the new pending ids (a
    merged one names the item it was merged into only through the fold's aliases).

    Raises:
        OpenSessionError: an unended session and no `live` that reaches it.
    """
    if not doubts:
        return []
    events, ids = _doubt_events(doubts)
    await _commit_events(
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=events,
        message=f"Dudas nuevas en {subject_slug}/{topic_slug}: {len(ids)} del editor",
        live=live,
    )
    return ids


class AskedDoubt(_Strict):
    """A doubt asked in the chat (`ask_in_chat`): the `doubt.asked` payload plus where it went."""

    pending_id: str
    kind: str = ""
    text: str = Field(default="", description="What the doubt is about (its explanation).")
    question: str
    suggestions: list[str] = Field(default_factory=list)
    options: list[SourceOption] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    session_id: str | None = None
    commit: str | None = None


async def ask_in_chat(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    pending_id: str,
    *,
    sync: GitSync,
    host: str | None = None,
    live: LiveSink | None = None,
) -> AskedDoubt:
    """Ask one open doubt in the chat: its latest question again as a `pending.question` with
    `in_chat: true` (a generic question when it has none), in the live session or a review one.

    Raises:
        UnknownDoubtError, DoubtClosedError, OpenSessionError: nothing written.
    """
    item = await asyncio.to_thread(_open_item, vault, subject_slug, topic_slug, pending_id)
    questions, _ = await asyncio.to_thread(_read_records, vault, subject_slug, topic_slug)
    latest: DoubtQuestion | None = _latest(questions, item)
    question = (latest or _fallback_question(item, None)).model_copy(
        update={"pending_id": item.id, "in_chat": True, "asked_at": None}
    )
    session_id, commit = await _commit_events(
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=[_question_event(question)],
        message=f"Duda preguntada en el chat de {subject_slug}/{topic_slug}: {_short(item.text)}",
        live=live,
    )
    pages = await asyncio.to_thread(capture_pages, vault, subject_slug, topic_slug)
    return AskedDoubt(
        pending_id=item.id,
        kind=item.kind,
        text=item.text,
        question=question.question,
        suggestions=question.suggestions,
        options=question.options,
        refs=_item_refs(item, pages),
        session_id=session_id,
        commit=commit,
    )


# -- review ---------------------------------------------------------------------------------------


SETTLED_REVIEW_RULE = (
    "Los bloques marcados «[revisado]» ya los revisó el estudiante y no tienen dudas abiertas:"
    " no los cambies ni los borres. Una duda sobre algo que un bloque «[revisado]» ya dice (la"
    " misma idea, aunque otra captura de la misma página la lea distinto o con una marca de"
    " duda) no se le pregunta: resuélvela con `auto_resolve`, sin ediciones, dando como prueba"
    " la fuente que cita ese bloque y lo que dice."
)
"""The review's rule about settled blocks (#474), given when the block map marks any."""


def _review_instruction(items: list[PendingItem], notes: str, settled: Collection[str] = ()) -> str:
    listing = "\n".join(_item_line(item) for item in items)
    blocks = describe_sections(notes, settled)
    rule = f"\n{SETTLED_REVIEW_RULE}\n" if settled and SETTLED_MARK in blocks else ""
    return (
        "## Tarea: revisar las dudas abiertas (herramienta `resolve_doubts`)\n\n"
        "En esta tarea sí resuelves las dudas abiertas que tus fuentes contestan, citándolas, y"
        " preguntas al estudiante las demás. Da una decisión por cada una de estas dudas:\n\n"
        f"{listing}\n\n"
        "Mapa de bloques de los apuntes actuales (para las ediciones):\n\n"
        f"{blocks}\n{rule}"
    )


def _check_review(
    value: DoubtsReviewOutput,
    items: list[PendingItem],
    assembled: EditorInput,
    vault: Vault,
    notes: str,
    settled: Collection[str] = (),
    doubted: Collection[str] = (),
) -> list[str]:
    """Spanish errors of a review; its edits must not change or delete a settled block of
    `notes` (`settled`, #474), nor make one cite a source an open doubt names (`doubted`, #483)."""
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
    return [
        *settled_block_errors(notes, edited, settled, doubted),
        *_notes_errors(assembled, vault, edited, notes),
    ]


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


def _review_summary(decisions: list[DoubtDecision]) -> str | None:
    if not decisions:
        return None
    resolutions = "; ".join((d.resolution or "").strip().rstrip(".") for d in decisions)
    count = len(decisions)
    noun = "duda" if count == 1 else "dudas"
    return f"He resuelto con tus fuentes {count} {noun}: {resolutions}."


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
    pending_ids: Sequence[str] | None = None,
    live: LiveSink | None = None,
) -> ReviewResult:
    """Review the open doubts of the topic with the editor (see the module docstring).

    `pending_ids` limits the review to those open doubts (the chat reviews the ones it is about
    to ask); nothing is sent when no doubt is open. The auto-resolutions' edits are applied under
    the topic's write lock on the latest notes: when the student saved meanwhile, the editor is
    re-asked with the new block map. With an unended session of the topic the events go to it
    through `live`.

    Raises:
        NotesMissingError: the topic has no notes yet.
        OpenSessionError: the topic has an unended session and no `live`.
        CostConfirmationRequiredError, RefusalError, LLMError: as `generate_notes`; nothing written.
        VaultError, ObserverStateError: the topic cannot be read.
    """
    await asyncio.to_thread(_require_writable, vault, subject_slug, topic_slug, live)
    items = (await asyncio.to_thread(_state, vault, subject_slug, topic_slug)).open_pending()
    if pending_ids is not None:
        wanted = set(pending_ids)
        items = [item for item in items if item.id in wanted]
    if not items:
        return ReviewResult(subject=subject_slug, topic=topic_slug)
    base = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)
    if not base or not base.strip():
        raise NotesMissingError(
            "Todavía no hay apuntes de este tema: prepáralos antes de revisar las dudas."
        )
    assembled, prompt_hash = await _assemble(
        vault,
        subject_slug,
        topic_slug,
        _review_instruction(
            items,
            base,
            await asyncio.to_thread(settled_blocks, vault, subject_slug, topic_slug, base),
        ),
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
    notes = {"base": base, "edited": base}
    notes_commit: list[str | None] = []

    def check(value: DoubtsReviewOutput) -> list[str]:
        last[:] = [value]
        settled = settled_blocks(vault, subject_slug, topic_slug, notes["base"])
        doubted = open_doubt_sources(vault, subject_slug, topic_slug)
        return _check_review(value, items, assembled, vault, notes["base"], settled, doubted)

    async def apply(value: DoubtsReviewOutput) -> str | None:
        edits = [e for d in value.decisions if d.action == "auto_resolve" for e in d.edits]
        edited = apply_edits(notes["base"], edits, value.footnotes)
        if edited == notes["base"]:
            return None
        current, written = await asyncio.to_thread(
            _write_notes_if_current,
            vault,
            sync,
            subject_slug,
            topic_slug,
            notes["base"],
            edited,
            f"Apuntes de {subject_slug}/{topic_slug}: dudas resueltas con fuentes",
        )
        if current is not None:
            notes["base"] = current
            return current
        notes["edited"] = edited
        notes_commit[:] = [written]
        return None

    value, attempts, model, _errors = await task.run(
        DoubtsReviewOutput,
        REVIEW_TOOL,
        "Record one decision per open doubt: auto-resolve it with cited evidence and edits, or"
        " ask the student.",
        check,
        apply,
    )

    events: list[_Event] = []
    auto: list[DoubtDecision] = []
    asked: list[str] = []
    changed = notes["edited"] != notes["base"]
    by_id = {decision.pending_id: decision for decision in (last[0].decisions if last else [])}
    for item in items:
        decision = by_id.get(item.id)
        if value is not None and decision is not None and decision.action == "auto_resolve":
            outcome = DoubtOutcome(
                pending_id=item.id,
                status="auto_resolved",
                resolution=decision.resolution,
                evidence=decision.evidence,
                notes_changed=bool(decision.edits) and changed,
            )
            events.extend(_close_events(item, outcome, "editor"))
            auto.append(decision)
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
        events.append(_question_event(question))
        asked.append(item.id)
    warning = None
    if value is None:
        warning = (
            "El editor no ha podido resolver las dudas citando tus fuentes; quedan todas como"
            " preguntas y los apuntes no han cambiado."
        )
    message = (
        f"Dudas de {subject_slug}/{topic_slug} revisadas: {len(auto)} resueltas con fuentes,"
        f" {len(asked)} preguntas"
    )
    session_id, commit = await _commit_events(
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=events,
        message=message,
        live=live,
    )
    result = ReviewResult(
        subject=subject_slug,
        topic=topic_slug,
        auto_resolved=[decision.pending_id for decision in auto],
        asked=asked,
        notes_changed=changed,
        summary=_review_summary(auto),
        revision=await asyncio.to_thread(_stored_revision, vault, subject_slug, topic_slug),
        session_id=session_id,
        commit=commit or next(iter(notes_commit), None),
        attempts=attempts,
        warning=warning,
        model=model,
    )
    await task.conversation.record(
        PENDING_REVIEWED_RECORD,
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
    settled: Collection[str] = (),
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
        describe_sections(notes, settled),
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
    live: LiveSink | None = None,
) -> ResolutionResult:
    """Record the student's answer to one doubt and apply it to the notes with the editor.

    The edits are applied under the topic's short write lock on the latest notes: when they
    changed since the editor's block map was built (a student save), nothing is applied and the
    editor is re-asked with the new block map, like a revision turn. With an unended session of
    the topic the events go to it through `live`.

    Raises:
        UnknownDoubtError, DoubtClosedError, InvalidAnswerError, OpenSessionError: nothing sent.
        CostConfirmationRequiredError, RefusalError, LLMError: nothing written.
    """
    await asyncio.to_thread(_require_writable, vault, subject_slug, topic_slug, live)
    item = await asyncio.to_thread(_open_item, vault, subject_slug, topic_slug, pending_id)
    questions, _ = await asyncio.to_thread(_read_records, vault, subject_slug, topic_slug)
    question = _latest(questions, item)
    outcome, decision = _decision_text(item, question, answer)
    base = await asyncio.to_thread(read_notes, vault, subject_slug, topic_slug)

    attempts, model = 0, None
    changed = False
    notes_commit: list[str | None] = []
    resolution = decision
    conversation = _Conversation(vault, subject_slug, topic_slug, clock)
    prompt_hash = None
    if base and base.strip():
        assembled, prompt_hash = await _assemble(
            vault,
            subject_slug,
            topic_slug,
            _answer_instruction(
                item,
                question,
                outcome,
                decision,
                base,
                await asyncio.to_thread(settled_blocks, vault, subject_slug, topic_slug, base),
            ),
            digest,
            max_page_images,
            max_attachment_bytes,
        )
        task = _Task(client, assembled, conversation, prompt_hash, "doubt_answer", confirm_over_cap)
        notes = {"base": base}

        def check(value: DecisionOutput) -> list[str]:
            if not value.resolution.strip():
                return ["Falta la resolución."]
            try:
                edited = apply_edits(notes["base"], value.edits, value.footnotes)
            except EditError as error:
                return error.errors
            return _notes_errors(assembled, vault, edited, notes["base"])

        async def apply(value: DecisionOutput) -> str | None:
            nonlocal changed
            edited = apply_edits(notes["base"], value.edits, value.footnotes)
            if edited == notes["base"]:
                return None
            current, written = await asyncio.to_thread(
                _write_notes_if_current,
                vault,
                sync,
                subject_slug,
                topic_slug,
                notes["base"],
                edited,
                f"Apuntes de {subject_slug}/{topic_slug}: duda resuelta: {_short(item.text)}",
            )
            if current is not None:
                notes["base"] = current
                return current
            changed = True
            notes_commit[:] = [written]
            return None

        value, attempts, model, _errors = await task.run(
            DecisionOutput,
            DECISION_TOOL,
            "Record the student's decision on the doubt and the edits that apply it to the notes.",
            check,
            apply,
        )
        if value is None:
            outcome.warning = (
                "La decisión queda guardada, pero el editor no ha podido aplicarla a los apuntes"
                + (
                    " porque los has cambiado mientras respondía: pídeselo en el chat."
                    if task.stale
                    else " cumpliendo las reglas de procedencia: revísalos o pídeselo en el chat."
                )
            )
        else:
            resolution = value.resolution.strip()
    outcome.resolution = resolution
    outcome.notes_changed = changed
    session_id, commit = await _commit_events(
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=_close_events(item, outcome, "user"),
        message=f"Duda resuelta en {subject_slug}/{topic_slug}: {_short(item.text)}",
        live=live,
    )
    if await asyncio.to_thread(_record_closed, vault, subject_slug, topic_slug, item, clock):
        sync.note_change()
    result = ResolutionResult(
        subject=subject_slug,
        topic=topic_slug,
        pending_id=item.id,
        status="resolved",
        resolution=resolution,
        notes_changed=outcome.notes_changed,
        revision=await asyncio.to_thread(_stored_revision, vault, subject_slug, topic_slug),
        session_id=session_id,
        commit=commit or next(iter(notes_commit), None),
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


def _record_closed(
    vault: Vault, subject_slug: str, topic_slug: str, item: PendingItem, clock: Clock
) -> bool:
    """The student closed `item`: the blocks now citing its sources are reviewed (#474)."""
    try:
        sources = item_sources(
            item,
            capture_pages(vault, subject_slug, topic_slug),
            _state(vault, subject_slug, topic_slug),
        )
        notes = read_notes(vault, subject_slug, topic_slug)
        blocks = blocks_citing(notes, sources)
    except Exception:
        logger.exception(
            "could not find the blocks a closed doubt of %s/%s reviews", subject_slug, topic_slug
        )
        return False
    return record_reviewed(
        vault, subject_slug, topic_slug, "doubt_closed", blocks, notes=notes, clock=clock
    )


async def dismiss_doubt(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    pending_id: str,
    *,
    sync: GitSync,
    host: str | None = None,
    live: LiveSink | None = None,
    clock: Clock = _utc_now,
) -> ResolutionResult:
    """Discard one doubt: closed as `dismissed`, no call, the notes untouched.

    Raises:
        UnknownDoubtError, DoubtClosedError, OpenSessionError: nothing written.
    """
    await asyncio.to_thread(_require_writable, vault, subject_slug, topic_slug, live)
    item = await asyncio.to_thread(_open_item, vault, subject_slug, topic_slug, pending_id)
    outcome = DoubtOutcome(pending_id=item.id, status="dismissed")
    session_id, commit = await _commit_events(
        vault,
        sync,
        subject_slug,
        topic_slug,
        host=host or socket.gethostname(),
        events=_close_events(item, outcome, "user"),
        message=f"Duda descartada en {subject_slug}/{topic_slug}: {_short(item.text)}",
        live=live,
    )
    if await asyncio.to_thread(_record_closed, vault, subject_slug, topic_slug, item, clock):
        sync.note_change()
    return ResolutionResult(
        subject=subject_slug,
        topic=topic_slug,
        pending_id=item.id,
        status="dismissed",
        revision=await asyncio.to_thread(_stored_revision, vault, subject_slug, topic_slug),
        session_id=session_id,
        commit=commit,
    )


def _short(text: str, width: int = 60) -> str:
    words = " ".join(text.split())
    return words if len(words) <= width else words[:width].rstrip() + "…"


__all__ = [
    "DECISION_TOOL",
    "MAX_EDITOR_DOUBTS",
    "MAX_REASKS",
    "PENDING_QUESTION_KIND",
    "PENDING_RESOLVED_KIND",
    "PENDING_REVIEWED_RECORD",
    "REVIEW_TOOL",
    "AskPlan",
    "AskedDoubt",
    "DecisionOutput",
    "Doubt",
    "DoubtAnswer",
    "DoubtChatTurn",
    "DoubtClosedError",
    "DoubtDecision",
    "DoubtError",
    "DoubtOutcome",
    "DoubtQuestion",
    "DoubtsQueue",
    "DoubtsReviewOutput",
    "EditorDoubt",
    "Evidence",
    "InvalidAnswerError",
    "LiveSink",
    "NotesMissingError",
    "OpenSessionError",
    "ResolutionResult",
    "ReviewResult",
    "SourceOption",
    "UnknownDoubtError",
    "answer_doubt",
    "ask_in_chat",
    "ask_plan",
    "dismiss_doubt",
    "doubt_chat_turns",
    "editor_doubt_errors",
    "list_doubts",
    "open_doubt",
    "raise_doubts",
    "review_doubts",
]
