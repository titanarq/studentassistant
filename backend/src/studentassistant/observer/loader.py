"""Load a topic's observer state from the vault: snapshot + the events after it, folded.

The only I/O of the observer's state, and all of it through the vault (ADR-0002): the events come
from `read_topic_events` and the snapshot from `read_observer_snapshot` / `write_observer_snapshot`.
This module opens no file and runs no git itself.
"""

from __future__ import annotations

from studentassistant.observer.fold import TopicEvent
from studentassistant.observer.snapshot import STATE_VERSION, ObserverSnapshot, advance_snapshot
from studentassistant.observer.state import EventRef
from studentassistant.vault import (
    SnapshotFileError,
    Vault,
    read_observer_snapshot,
    read_topic_events,
    write_observer_snapshot,
)


def _still_valid(snapshot: ObserverSnapshot, events: list[TopicEvent]) -> bool:
    """Whether `snapshot` is the fold of exactly the first `event_count` of `events`.

    It is not when the fold changed meaning (`state_version`) or when the log changed under it:
    a session pulled from another PC with an earlier id, or events appended to a session before
    the cursor's, shift which event is the `event_count`-th.
    """
    if snapshot.state_version != STATE_VERSION or snapshot.event_count > len(events):
        return False
    if snapshot.event_count == 0:
        return snapshot.cursor is None
    session_id, event = events[snapshot.event_count - 1]
    return snapshot.cursor == EventRef(session_id=session_id, seq=event.seq)


def load_observer_snapshot(
    vault: Vault, subject_slug: str, topic_slug: str, *, write_back: bool = True
) -> ObserverSnapshot:
    """The topic's up-to-date observer snapshot; the stored one is refreshed when it changed.

    The stored snapshot is used when it still matches the log; otherwise (none stored, not
    readable, another `state_version`, or the log changed before its cursor) the state is folded
    from scratch. The events after it are folded and, when the result differs from what was
    stored, it is written back through `write_observer_snapshot`, unless `write_back` is false
    (a read-only caller, such as a listing, that must leave the vault untouched).

    Raises:
        SubjectNotFoundError, TopicNotFoundError, SessionFileError: what the vault raises for a
            topic or a session it cannot read.
        ObserverStateError: an event of the log cannot be folded (see `fold`).
    """
    events = list(read_topic_events(vault, subject_slug, topic_slug))
    try:
        stored = read_observer_snapshot(vault, subject_slug, topic_slug, ObserverSnapshot)
    except SnapshotFileError:
        stored = None
    base = stored if stored is not None and _still_valid(stored, events) else None
    start = base.event_count if base is not None else 0
    snapshot = advance_snapshot(base, events[start:])
    if write_back and snapshot != stored:
        write_observer_snapshot(vault, subject_slug, topic_slug, snapshot)
    return snapshot
