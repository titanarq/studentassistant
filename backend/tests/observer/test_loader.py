"""`load_observer_snapshot`: events and snapshot read, and the snapshot written, via the vault."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studentassistant.observer import (
    ObserverSnapshot,
    TopicEvent,
    UnknownIdError,
    fold,
    load_observer_snapshot,
)
from studentassistant.observer import loader as loader_module
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    end_session,
    read_observer_snapshot,
    read_topic_events,
    start_session,
    write_observer_snapshot,
)
from studentassistant.vault.state import observer_snapshot_path

from .script import ScriptedEvent, op, to_topic_events

Script = list[tuple[str, list[ScriptedEvent]]]


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def record(session: Session, events: list[ScriptedEvent]) -> None:
    for origin, kind, payload in events:
        session.append_event(kind, origin, payload)


def record_script(vault: Vault, topic: tuple[str, str], script: Script) -> Script:
    """Record every session of `script` in the vault; return it under the real session ids."""
    recorded: Script = []
    for _, events in script:
        session = start_session(vault, *topic, host="ubuntu-pc", protocol_version="1.0")
        record(session, events)
        end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
        recorded.append((session.meta.id, events))
    return recorded


def test_loader_folds_the_vault_events_and_stores_the_snapshot(
    tmp_vault: Vault, topic: tuple[str, str], scripted_sessions: Script
) -> None:
    recorded = record_script(tmp_vault, topic, scripted_sessions)

    snapshot = load_observer_snapshot(tmp_vault, *topic)

    assert snapshot.state == fold(to_topic_events(recorded))
    assert snapshot.state == fold(read_topic_events(tmp_vault, *topic))
    assert read_observer_snapshot(tmp_vault, *topic, ObserverSnapshot) == snapshot


def test_loading_again_without_new_events_writes_nothing(
    tmp_vault: Vault, topic: tuple[str, str], scripted_sessions: Script
) -> None:
    record_script(tmp_vault, topic, scripted_sessions)
    first = load_observer_snapshot(tmp_vault, *topic)
    path = observer_snapshot_path(tmp_vault, *topic)
    written = path.stat().st_mtime_ns

    assert load_observer_snapshot(tmp_vault, *topic) == first
    assert path.stat().st_mtime_ns == written


def test_loader_folds_only_the_tail_after_the_stored_snapshot(
    tmp_vault: Vault,
    topic: tuple[str, str],
    scripted_sessions: Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_session = scripted_sessions[:1]
    record_script(tmp_vault, topic, first_session)
    before = load_observer_snapshot(tmp_vault, *topic)
    record_script(tmp_vault, topic, scripted_sessions[1:])

    tails: list[int] = []
    real_advance = loader_module.advance_snapshot

    def spying_advance(
        snapshot: ObserverSnapshot | None, tail: list[TopicEvent]
    ) -> ObserverSnapshot:
        tails.append(len(tail))
        return real_advance(snapshot, tail)

    monkeypatch.setattr(loader_module, "advance_snapshot", spying_advance)
    after = load_observer_snapshot(tmp_vault, *topic)

    assert tails == [len(scripted_sessions[1][1])]
    assert after.event_count == before.event_count + tails[0]
    assert after.state == fold(read_topic_events(tmp_vault, *topic))


def test_an_unreadable_or_stale_snapshot_is_folded_again(
    tmp_vault: Vault, topic: tuple[str, str], scripted_sessions: Script
) -> None:
    record_script(tmp_vault, topic, scripted_sessions)
    expected = load_observer_snapshot(tmp_vault, *topic)
    path = observer_snapshot_path(tmp_vault, *topic)

    path.write_text("{not json", encoding="utf-8")
    assert load_observer_snapshot(tmp_vault, *topic) == expected

    write_observer_snapshot(tmp_vault, *topic, expected.model_copy(update={"state_version": 0}))
    assert load_observer_snapshot(tmp_vault, *topic) == expected

    lying = expected.model_copy(update={"event_count": expected.event_count - 1})
    write_observer_snapshot(tmp_vault, *topic, lying)
    assert load_observer_snapshot(tmp_vault, *topic) == expected


def test_an_invalid_op_in_the_log_raises(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    session = start_session(tmp_vault, *topic, host="ubuntu-pc", protocol_version="1.0")
    record(session, [op("rename_section", section_id="nope", title="x")])

    with pytest.raises(UnknownIdError):
        load_observer_snapshot(tmp_vault, *topic)
    assert read_observer_snapshot(tmp_vault, *topic, ObserverSnapshot) is None
