"""Smoke test: the scripted two-session fixture loads as `(session_id, Event)` pairs."""

from __future__ import annotations

from studentassistant.observer import STATE_OP_EVENT_KIND, TopicEvent

from .script import FIRST_SESSION, SECOND_SESSION


def test_fixture_covers_two_sessions_with_per_session_seq(topic_events: list[TopicEvent]) -> None:
    sessions = [session_id for session_id, _ in topic_events]
    assert sorted(set(sessions)) == [FIRST_SESSION, SECOND_SESSION]
    for session_id in (FIRST_SESSION, SECOND_SESSION):
        seqs = [event.seq for sid, event in topic_events if sid == session_id]
        assert seqs == list(range(1, len(seqs) + 1))
    assert any(event.kind == STATE_OP_EVENT_KIND for _, event in topic_events)
    assert any(event.kind == "marker" for _, event in topic_events)
