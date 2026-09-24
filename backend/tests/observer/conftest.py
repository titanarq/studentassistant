"""A scripted two-session topic: the events a live observer would have produced for "Derivadas".

`scripted_sessions` is the raw script (session id and the `(origin, kind, payload)` of each event,
`seq` implied by position); `topic_events` is the same script as the `(session_id, Event)` pairs
`read_topic_events` yields. Besides state ops it holds the fact events that register segments and
captures, and events of kinds the fold ignores. The script and its helpers live in `script.py`.
"""

from __future__ import annotations

import pytest

from studentassistant.observer import TopicEvent

from .script import SCRIPT, ScriptedEvent, to_topic_events


@pytest.fixture
def scripted_sessions() -> list[tuple[str, list[ScriptedEvent]]]:
    return [(session_id, list(events)) for session_id, events in SCRIPT]


@pytest.fixture
def topic_events(scripted_sessions: list[tuple[str, list[ScriptedEvent]]]) -> list[TopicEvent]:
    return to_topic_events(scripted_sessions)
