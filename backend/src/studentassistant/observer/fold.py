"""The pure fold of a topic's session events into its `TopicState` (ADR-0003).

`fold(events)` takes the `(session_id, Event)` pairs of every session of a topic, in the order
`studentassistant.vault.read_topic_events` yields them, and returns the state. It reads no file,
calls no model and depends on nothing but its input, so replaying the same events always gives
the same state; a new observer version is tested by replaying recorded sessions through it.

Three event kinds matter to the fold; every other kind is ignored:
- `STATE_OP_EVENT_KIND` (`observer.state_op`): the payload is one state op (`ops.parse_op`).
- `SEGMENT_EVENT_KIND` (`transcript.final`): a final transcript segment was stored; its
  `payload["segment_id"]` becomes a segment id ops may reference.
- `CAPTURE_EVENT_KIND` (`capture.stored`): a capture was stored; its `payload["capture_id"]`
  becomes a capture id ops may reference.

An op is validated against the state before it is applied (`validate_op`); an op that references
an unknown id is never skipped silently: `fold` raises the typed error.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, assert_never

from pydantic import ValidationError

from studentassistant.observer.ops import (
    STATE_OP_EVENT_KIND,
    AddConcept,
    AddPending,
    AddSection,
    AssignSegments,
    LinkCapture,
    Note,
    RenameSection,
    ResolvePending,
    SetSourceContext,
    StateOp,
    parse_op,
)
from studentassistant.observer.state import (
    Concept,
    EventRef,
    ObserverNote,
    PendingItem,
    Section,
    SourceContext,
    TopicState,
)
from studentassistant.vault import Event

SEGMENT_EVENT_KIND = "transcript.final"
SEGMENT_ID_KEY = "segment_id"
CAPTURE_EVENT_KIND = "capture.stored"
CAPTURE_ID_KEY = "capture_id"

TopicEvent = tuple[str, Event]


class ObserverStateError(ValueError):
    """An event the fold cannot apply; `at` is that event once the fold knows which one it is."""

    def __init__(self, message: str, at: EventRef | None = None) -> None:
        super().__init__(message)
        self.at = at

    def __str__(self) -> str:
        base = super().__str__()
        if self.at is None:
            return base
        return f"{base} (session {self.at.session_id}, seq {self.at.seq})"


class UnknownIdError(ObserverStateError):
    """An op references a section, segment, capture or pending item the state does not have."""

    def __init__(self, op: str, entity: str, id: str) -> None:
        super().__init__(f"{op}: unknown {entity} {id!r}")
        self.op = op
        self.entity = entity
        self.id = id


class DuplicateIdError(ObserverStateError):
    """An op creates a section, concept or pending item under an id that already exists."""

    def __init__(self, op: str, entity: str, id: str) -> None:
        super().__init__(f"{op}: {entity} {id!r} already exists")
        self.op = op
        self.entity = entity
        self.id = id


class PendingAlreadyResolvedError(ObserverStateError):
    """`resolve_pending` on an item that was already resolved."""

    def __init__(self, id: str) -> None:
        super().__init__(f"resolve_pending: pending item {id!r} is already resolved")
        self.id = id


class InvalidEventError(ObserverStateError):
    """An event of a kind the fold reads whose payload is not what that kind carries."""


class EventOrderError(ObserverStateError):
    """Events not strictly after one another in `(session_id, seq)` order (or after a cursor)."""


def _require(known: dict[str, Any], op: str, entity: str, ids: Iterable[str]) -> None:
    for id in ids:
        if id not in known:
            raise UnknownIdError(op, entity, id)


def validate_op(state: TopicState, op: StateOp) -> None:
    """Check that `op` can be applied to `state`; raise the typed error when it cannot.

    Raises:
        UnknownIdError: an id the op references is not in the state.
        DuplicateIdError: the op creates something under an id already taken.
        PendingAlreadyResolvedError: the op resolves an item already resolved.
    """
    match op:
        case AddSection():
            if op.section_id in state.sections:
                raise DuplicateIdError(op.op, "section", op.section_id)
            if op.parent_id is not None:
                _require(state.sections, op.op, "section", [op.parent_id])
        case RenameSection():
            _require(state.sections, op.op, "section", [op.section_id])
        case AssignSegments():
            _require(state.sections, op.op, "section", [op.section_id])
            _require(state.segments, op.op, "segment", op.segment_ids)
        case AddConcept():
            if op.concept_id in state.concepts:
                raise DuplicateIdError(op.op, "concept", op.concept_id)
            if op.section_id is not None:
                _require(state.sections, op.op, "section", [op.section_id])
            _require(state.segments, op.op, "segment", op.segment_ids)
        case LinkCapture():
            _require(state.captures, op.op, "capture", [op.capture_id])
            _require(state.segments, op.op, "segment", op.segment_ids)
        case SetSourceContext():
            pass
        case AddPending():
            if op.pending_id in state.pending:
                raise DuplicateIdError(op.op, "pending item", op.pending_id)
            _require(state.segments, op.op, "segment", op.segment_ids)
            _require(state.captures, op.op, "capture", op.capture_ids)
        case ResolvePending():
            _require(state.pending, op.op, "pending item", [op.pending_id])
            if not state.pending[op.pending_id].is_open:
                raise PendingAlreadyResolvedError(op.pending_id)
        case Note():
            _require(state.segments, op.op, "segment", op.segment_ids)
        case _:
            assert_never(op)


def _unique(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _apply_in_place(state: TopicState, op: StateOp, at: EventRef) -> None:
    """Apply a validated `op` to `state`, mutating it; only the fold's own copy is passed."""
    match op:
        case AddSection():
            state.sections[op.section_id] = Section(
                id=op.section_id, title=op.title, parent_id=op.parent_id, added_at=at
            )
        case RenameSection():
            state.sections[op.section_id].title = op.title
        case AssignSegments():
            for segment_id in op.segment_ids:
                state.assignments[segment_id] = op.section_id
        case AddConcept():
            state.concepts[op.concept_id] = Concept(
                id=op.concept_id,
                name=op.name,
                section_id=op.section_id,
                segment_ids=_unique(op.segment_ids),
                added_at=at,
            )
        case LinkCapture():
            linked = state.capture_links.get(op.capture_id, [])
            state.capture_links[op.capture_id] = _unique([*linked, *op.segment_ids])
        case SetSourceContext():
            state.source_context = SourceContext(kind=op.kind, reference=op.reference, set_at=at)
        case AddPending():
            state.pending[op.pending_id] = PendingItem(
                id=op.pending_id,
                category=op.category,
                description=op.description,
                segment_ids=_unique(op.segment_ids),
                capture_ids=_unique(op.capture_ids),
                added_at=at,
            )
        case ResolvePending():
            item = state.pending[op.pending_id]
            item.resolution = op.resolution
            item.resolved_at = at
        case Note():
            state.notes.append(
                ObserverNote(text=op.text, segment_ids=_unique(op.segment_ids), added_at=at)
            )
        case _:
            assert_never(op)


def apply_op(state: TopicState, op: StateOp, at: EventRef) -> TopicState:
    """The state after `op`, emitted by the event `at`; `state` itself is left unchanged.

    Raises:
        ObserverStateError: the error `validate_op` raises; nothing is applied.
    """
    validate_op(state, op)
    new_state = state.model_copy(deep=True)
    _apply_in_place(new_state, op, at)
    return new_state


def _registered_id(event: Event, key: str, at: EventRef) -> str:
    value = event.payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidEventError(f"{event.kind} event without a {key}", at)
    return value


def _step(state: TopicState, session_id: str, event: Event) -> None:
    """Fold one event into `state` in place (the fold's own copy)."""
    at = EventRef(session_id=session_id, seq=event.seq)
    if event.kind == SEGMENT_EVENT_KIND:
        state.segments.setdefault(_registered_id(event, SEGMENT_ID_KEY, at), at)
    elif event.kind == CAPTURE_EVENT_KIND:
        state.captures.setdefault(_registered_id(event, CAPTURE_ID_KEY, at), at)
    elif event.kind == STATE_OP_EVENT_KIND:
        try:
            op = parse_op(event.payload)
        except ValidationError as error:
            raise InvalidEventError(f"invalid state op: {error}", at) from error
        try:
            validate_op(state, op)
        except ObserverStateError as error:
            error.at = at
            raise
        _apply_in_place(state, op, at)


def fold_into(
    state: TopicState, events: Iterable[TopicEvent], after: EventRef | None = None
) -> EventRef | None:
    """Fold `events` into `state` in place; return the ref of the last event folded.

    Internal to the observer (the snapshot uses it): public callers use `fold` or `fold_from`,
    which never mutate what they are given. Events must come strictly after `after` and after one
    another in `(session_id, seq)` order.
    """
    last = after
    for session_id, event in events:
        at = EventRef(session_id=session_id, seq=event.seq)
        if last is not None and at.key() <= last.key():
            raise EventOrderError(f"event not after session {last.session_id}, seq {last.seq}", at)
        _step(state, session_id, event)
        last = at
    return last


def fold(events: Iterable[TopicEvent]) -> TopicState:
    """The topic's state folded from scratch from `events`, the `(session_id, Event)` pairs of all
    its sessions in `(session_id, seq)` order.

    Raises:
        ObserverStateError: an op of the log cannot be applied (unknown or duplicate id, pending
            already resolved), a read event carries a malformed payload, or events are out of
            order; the error's `at` names the event.
    """
    state = TopicState()
    fold_into(state, events)
    return state
