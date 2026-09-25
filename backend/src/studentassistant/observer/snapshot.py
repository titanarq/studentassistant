"""The observer snapshot: a folded `TopicState` plus where the fold stopped (an optimisation).

Because `seq` restarts in every session, the cursor is the `(session_id, seq)` of the last event
folded. `fold_from(snapshot, tail)` continues the fold with the events after the cursor and gives
exactly what `fold` gives over all the events; the snapshot is never the source of truth and can
always be rebuilt from the log (ADR-0003). Pure code: the vault stores it (`loader`).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.observer.fold import TopicEvent, fold_into
from studentassistant.observer.state import EventRef, TopicState

# Bumped whenever the fold's meaning changes: a snapshot of another version is folded again from
# scratch rather than trusted.
STATE_VERSION = 2
# 2 (#55): pending items carry kind/text/refs/created_by/status, duplicates merge (aliases).


class ObserverSnapshot(BaseModel):
    """`state/observer-snapshot.json`: the state folded from the first `event_count` events of the
    topic, the last of them being `cursor` (`None` when nothing was folded yet)."""

    model_config = ConfigDict(extra="forbid")

    state_version: int = STATE_VERSION
    cursor: EventRef | None = None
    event_count: int = Field(default=0, ge=0)
    state: TopicState = Field(default_factory=TopicState)


def advance_snapshot(
    snapshot: ObserverSnapshot | None, tail: Iterable[TopicEvent]
) -> ObserverSnapshot:
    """A new snapshot: `snapshot` (an empty one for `None`) with the events of `tail` folded in.

    `snapshot` is left unchanged. Every event of `tail` must come after the snapshot's cursor.

    Raises:
        ObserverStateError: what `fold` raises, including `EventOrderError` for a tail event at or
            before the cursor.
    """
    base = snapshot if snapshot is not None else ObserverSnapshot()
    state = base.state.model_copy(deep=True)
    count = base.event_count

    def counted(events: Iterable[TopicEvent]) -> Iterable[TopicEvent]:
        nonlocal count
        for item in events:
            count += 1
            yield item

    cursor = fold_into(state, counted(tail), after=base.cursor)
    return ObserverSnapshot(
        state_version=base.state_version, cursor=cursor, event_count=count, state=state
    )


def fold_from(snapshot: ObserverSnapshot, tail: Iterable[TopicEvent]) -> TopicState:
    """The state after folding `tail` onto `snapshot`: equal to `fold` over all the events."""
    return advance_snapshot(snapshot, tail).state


def snapshot_of(events: Iterable[TopicEvent]) -> ObserverSnapshot:
    """The snapshot of folding `events` from scratch."""
    return advance_snapshot(None, events)
