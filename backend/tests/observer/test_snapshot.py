"""Snapshot + tail: folding from a snapshot at every split point equals folding from scratch."""

from __future__ import annotations

import pytest

from studentassistant.observer import (
    EventOrderError,
    EventRef,
    ObserverSnapshot,
    TopicEvent,
    advance_snapshot,
    fold,
    fold_from,
    snapshot_of,
)


def test_fold_from_every_split_point_equals_fold(topic_events: list[TopicEvent]) -> None:
    expected = fold(topic_events)
    for split in range(len(topic_events) + 1):
        snapshot = snapshot_of(topic_events[:split])
        # Through JSON, as the vault stores it.
        stored = ObserverSnapshot.model_validate_json(snapshot.model_dump_json())
        assert stored == snapshot
        assert fold_from(stored, topic_events[split:]) == expected, f"split at {split}"


def test_snapshots_chain_across_several_splits(topic_events: list[TopicEvent]) -> None:
    snapshot = ObserverSnapshot()
    for start in range(0, len(topic_events), 3):
        snapshot = advance_snapshot(snapshot, topic_events[start : start + 3])
    assert snapshot == snapshot_of(topic_events)
    assert snapshot.event_count == len(topic_events)


def test_cursor_is_the_last_event_folded(topic_events: list[TopicEvent]) -> None:
    assert snapshot_of([]).cursor is None
    session_id, event = topic_events[-1]
    snapshot = snapshot_of(topic_events)
    assert snapshot.cursor == EventRef(session_id=session_id, seq=event.seq)


def test_fold_from_does_not_mutate_the_snapshot(topic_events: list[TopicEvent]) -> None:
    snapshot = snapshot_of(topic_events[:5])
    before = snapshot.model_copy(deep=True)
    fold_from(snapshot, topic_events[5:])
    assert snapshot == before


def test_a_tail_event_at_or_before_the_cursor_raises(topic_events: list[TopicEvent]) -> None:
    snapshot = snapshot_of(topic_events[:5])
    with pytest.raises(EventOrderError):
        fold_from(snapshot, topic_events[4:])
