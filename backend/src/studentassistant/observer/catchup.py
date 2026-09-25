"""Catch-up: the batch items of a topic the observer never answered, found in its event log.

The live loop acknowledges every answered batch with a persisted `observer.ack` event
(`ACK_EVENT_KIND`, origin `observer`, published after the batch's ops) whose `payload.through` is
the `EventRef` (`session_id`, `seq`) of the newest event the batch carried: every batch-kind event
of the topic up to it, in `(session_id, seq)` order, has been answered. `through: null` answers
nothing; the loop writes it once, as the baseline, the first time it observes a topic that has no
acknowledgement yet (older topics are never replayed from their first session).

When the loop opens a session (a start, a resume, a restart after a crash), `unanswered` returns
what came after the newest acknowledgement and before the event that opened the session: the
last batch of an ended session whose call outlived the end hook, the tail of a session the
backend stopped without ending, the waiting items of a resumed one. The loop sends them first.
Delivery is at least once: a crash between a batch's ops and its acknowledgement sends the batch
again, and validation (duplicate ids) and the pending queue's merge absorb the repeat.

The vault purge (#31) compacts a topic's log only up to the newest acknowledgement's `through`
(`compactable_snapshot`): what comes after it, the newest `observer.ack` included (it is always
written after the events it answers), stays in the log as it was, so a purge never hides a batch
the observer still owes, on this PC or on any other that opens the topic later.

Pure: no I/O. The fold ignores `observer.ack`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from studentassistant.observer.context import BATCH_KINDS, OBSERVER_ORIGIN
from studentassistant.observer.fold import TopicEvent
from studentassistant.observer.ops import STATE_OP_EVENT_KIND
from studentassistant.observer.snapshot import ObserverSnapshot, snapshot_of
from studentassistant.observer.state import EventRef

ACK_EVENT_KIND = "observer.ack"
THROUGH_KEY = "through"


def ack_payload(through: EventRef | None) -> dict[str, Any]:
    """The payload of an `observer.ack` event answering everything up to `through`."""
    return {THROUGH_KEY: None if through is None else through.model_dump()}


def _through(payload: Mapping[str, Any]) -> EventRef | None:
    raw = payload.get(THROUGH_KEY)
    if raw is None:
        return None
    try:
        return EventRef.model_validate(raw)
    except ValidationError:
        return None


@dataclass(frozen=True)
class CatchUp:
    """What `unanswered` found.

    `acknowledged`: the topic has an `observer.ack` (else the loop writes the baseline);
    `through`: the newest acknowledged position (`None`: nothing answered yet); `events`: the
    unanswered batch-kind events, in topic order; `last`: the newest event before the opening
    one, the baseline's `through`.
    """

    acknowledged: bool = False
    through: EventRef | None = None
    events: list[TopicEvent] = field(default_factory=list)
    last: EventRef | None = None


def unanswered(
    events: Iterable[TopicEvent], *, session_id: str, before: EventRef | None
) -> CatchUp:
    """The batch-kind events of a topic after its newest acknowledgement and before `before`.

    `before` is the event that opened `session_id` for the observer: it and everything after it
    reach the loop on the bus. With `before` unknown (a transient event), no event of `session_id`
    is returned. The observer's own state ops are never returned (they are its answers).
    """
    acknowledged = False
    through: EventRef | None = None
    candidates: list[TopicEvent] = []
    last: EventRef | None = None
    for event_session, event in events:
        ref = EventRef(session_id=event_session, seq=event.seq)
        if event_session == session_id and (before is None or ref.key() >= before.key()):
            continue
        last = ref if last is None or ref.key() > last.key() else last
        if event.kind == ACK_EVENT_KIND:
            acknowledged = True
            acked = _through(event.payload)
            if acked is not None and (through is None or acked.key() > through.key()):
                through = acked
            continue
        if event.kind not in BATCH_KINDS:
            continue
        if event.kind == STATE_OP_EVENT_KIND and event.origin == OBSERVER_ORIGIN:
            continue
        candidates.append((event_session, event))
    if not acknowledged:
        return CatchUp(last=last)
    tail = [
        (event_session, event)
        for event_session, event in candidates
        if through is None
        or EventRef(session_id=event_session, seq=event.seq).key() > through.key()
    ]
    tail.sort(key=lambda pair: (pair[0], pair[1].seq))
    return CatchUp(acknowledged=True, through=through, events=tail, last=last)


def acknowledged_through(events: Iterable[TopicEvent]) -> EventRef | None:
    """The newest `through` any `observer.ack` of the topic names; `None` when none names one."""
    through: EventRef | None = None
    for _, event in events:
        if event.kind == ACK_EVENT_KIND:
            acked = _through(event.payload)
            if acked is not None and (through is None or acked.key() > through.key()):
                through = acked
    return through


def compactable_snapshot(events: Iterable[TopicEvent]) -> ObserverSnapshot | None:
    """The snapshot the vault purge may replace the start of the log with, or `None` for none.

    It folds every event up to the newest acknowledged position (`acknowledged_through`), so the
    events the observer has not answered yet -- and the acknowledgement that says so -- are left
    after the compaction's cursor. A topic with no acknowledgement naming an event (no observer
    yet, or only a `through: null` baseline) is not compacted at all.

    Raises:
        ObserverStateError: what `fold` raises for the folded prefix.
    """
    ordered = sorted(events, key=lambda pair: (pair[0], pair[1].seq))
    through = acknowledged_through(ordered)
    if through is None:
        return None
    prefix = [
        pair
        for pair in ordered
        if EventRef(session_id=pair[0], seq=pair[1].seq).key() <= through.key()
    ]
    return snapshot_of(prefix) if prefix else None


__all__ = [
    "ACK_EVENT_KIND",
    "THROUGH_KEY",
    "CatchUp",
    "ack_payload",
    "acknowledged_through",
    "compactable_snapshot",
    "unanswered",
]
