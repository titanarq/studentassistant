"""`unanswered`: what a topic's event log still owes the observer after its newest ack."""

from __future__ import annotations

from studentassistant.observer import STATE_OP_EVENT_KIND, EventRef, TopicEvent
from studentassistant.observer.catchup import ACK_EVENT_KIND, ack_payload, unanswered
from studentassistant.observer.snapshot import snapshot_of
from studentassistant.vault import Event

A, B = "20260924-180000", "20260925-180000"


def _event(seq: int, kind: str, origin: str = "stt", **payload: object) -> Event:
    return Event(seq=seq, t=seq * 1000, origin=origin, kind=kind, payload=dict(payload))  # type: ignore[arg-type]


def _seg(seq: int, segment_id: str) -> Event:
    return _event(seq, "transcript.final", segment_id=segment_id, text=segment_id)


def _ack(seq: int, through: EventRef | None) -> Event:
    return _event(seq, ACK_EVENT_KIND, "observer", **ack_payload(through))


def _ids(events: list[TopicEvent]) -> list[tuple[str, int]]:
    return [(session_id, event.seq) for session_id, event in events]


def test_a_topic_without_an_ack_owes_nothing_and_names_its_last_event() -> None:
    events = [(A, _event(1, "session.started", "user")), (A, _seg(2, "a")), (A, _seg(3, "b"))]
    found = unanswered(events, session_id=B, before=EventRef(session_id=B, seq=1))
    assert not found.acknowledged
    assert found.events == []
    assert found.last == EventRef(session_id=A, seq=3)


def test_the_events_after_the_newest_ack_are_owed_across_sessions() -> None:
    events: list[TopicEvent] = [
        (A, _event(1, "session.started", "user")),
        (A, _ack(2, None)),
        (A, _seg(3, "a")),
        (A, _event(4, STATE_OP_EVENT_KIND, "observer", op="note", text="x", segment_ids=[])),
        (A, _ack(5, EventRef(session_id=A, seq=3))),
        (A, _seg(6, "b")),  # the last batch, its call outlived the end
        (A, _event(7, "capture.stored", "phone", capture_id="c1")),
        (A, _event(8, STATE_OP_EVENT_KIND, "user", op="note", text="y", segment_ids=[])),
        (A, _event(9, "session.ended", "user")),
        (B, _event(1, "session.started", "user")),
    ]
    found = unanswered(events, session_id=B, before=EventRef(session_id=B, seq=1))
    assert found.acknowledged
    assert found.through == EventRef(session_id=A, seq=3)
    # Segments, captures and the student's ops; never the observer's ops, acks or lifecycle.
    assert _ids(found.events) == [(A, 6), (A, 7), (A, 8)]


def test_the_opening_event_and_what_follows_it_are_left_to_the_bus() -> None:
    events: list[TopicEvent] = [
        (A, _ack(1, None)),
        (A, _seg(2, "a")),
        (A, _event(3, "session.resumed", "user")),
        (A, _seg(4, "b")),
    ]
    found = unanswered(events, session_id=A, before=EventRef(session_id=A, seq=3))
    assert _ids(found.events) == [(A, 2)]
    # With the opening event unknown, the session's own events are all left out.
    assert unanswered(events, session_id=A, before=None).events == []


def test_the_newest_ack_wins_whatever_its_place_in_the_log() -> None:
    events: list[TopicEvent] = [
        (A, _seg(1, "a")),
        (A, _seg(2, "b")),
        (B, _ack(1, EventRef(session_id=A, seq=2))),
        (B, _ack(2, EventRef(session_id=A, seq=1))),
        (B, _seg(3, "c")),
    ]
    found = unanswered(events, session_id=B, before=EventRef(session_id=B, seq=4))
    assert found.through == EventRef(session_id=A, seq=2)
    assert _ids(found.events) == [(B, 3)]


def test_the_fold_ignores_acks() -> None:
    events: list[TopicEvent] = [(A, _seg(1, "a")), (A, _ack(2, EventRef(session_id=A, seq=1)))]
    assert snapshot_of(events).state == snapshot_of(events[:1]).state
