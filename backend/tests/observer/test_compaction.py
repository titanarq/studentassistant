"""The purge's event compaction: snapshot + remaining events still fold to the same state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studentassistant.config import VaultGitSettings, VaultPurgeSettings
from studentassistant.observer import (
    COMPACTED_EVENT_KIND,
    STATE_VERSION,
    InvalidEventError,
    ObserverSnapshot,
    TopicEvent,
    compaction_payload,
    fold,
    fold_from,
    load_observer_snapshot,
    snapshot_of,
)
from studentassistant.vault import (
    Event,
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    read_topic_events,
    start_session,
    topic_directory,
)
from studentassistant.vault.purge import Compaction, apply_purge, plan_topic_purge

from .script import ScriptedEvent, op, segment

Script = list[tuple[str, list[ScriptedEvent]]]
POLICY = VaultPurgeSettings(require_notes_tag=False)


@pytest.fixture
def topic(tmp_vault: Vault, scripted_sessions: Script) -> tuple[str, str]:
    """The scripted two-session topic recorded in the vault, ended, with notes."""
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    slug = create_topic(tmp_vault, subject, "Derivadas").slug
    for _, events in scripted_sessions:
        session = start_session(tmp_vault, subject, slug, host="ubuntu-pc", protocol_version="1.0")
        for origin, kind, payload in events:
            session.append_event(kind, origin, payload)
        end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
    notes = topic_directory(tmp_vault, subject, slug) / "notes" / "apuntes.md"
    notes.parent.mkdir()
    notes.write_text("# Derivadas\n", encoding="utf-8")
    return subject, slug


def compaction_of(snapshot: ObserverSnapshot) -> Compaction:
    assert snapshot.cursor is not None
    return Compaction(
        snapshot.cursor.session_id,
        snapshot.cursor.seq,
        COMPACTED_EVENT_KIND,
        compaction_payload(snapshot),
    )


def purge(vault: Vault, topic: tuple[str, str], compaction: Compaction) -> None:
    sync = GitSync(vault, VaultGitSettings())
    plan = plan_topic_purge(sync, *topic, POLICY, compaction)
    assert plan.compacts_events
    apply_purge(sync, [plan])


@pytest.mark.parametrize("folded", [1, 5, 14, 16, 20, 999])
def test_fold_after_compaction_equals_the_pre_purge_state(
    tmp_vault: Vault, topic: tuple[str, str], folded: int
) -> None:
    before = list(read_topic_events(tmp_vault, *topic))
    expected = fold(before)
    snapshot = snapshot_of(before[:folded])

    purge(tmp_vault, topic, compaction_of(snapshot))

    after = list(read_topic_events(tmp_vault, *topic))
    remaining = before[min(folded, len(before)) :]
    assert snapshot.cursor is not None
    assert after[0][1].kind == COMPACTED_EVENT_KIND
    assert (after[0][0], after[0][1].seq) == (snapshot.cursor.session_id, snapshot.cursor.seq)
    assert after[1:] == remaining
    assert fold(after) == expected
    assert fold_from(snapshot, remaining) == expected
    assert load_observer_snapshot(tmp_vault, *topic).state == expected


def test_new_events_after_a_purge_still_reference_what_was_folded(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    purge(
        tmp_vault, topic, compaction_of(load_observer_snapshot(tmp_vault, *topic, write_back=False))
    )
    session = start_session(tmp_vault, *topic, host="ubuntu-pc", protocol_version="1.0")
    for origin, kind, payload in [
        segment("s3-a"),
        op("assign_segments", section_id="sec-def", segment_ids=["s1-a", "s3-a"]),
    ]:
        session.append_event(kind, origin, payload)

    state = load_observer_snapshot(tmp_vault, *topic).state

    assert state.assignments["s1-a"] == "sec-def"
    assert state.assignments["s3-a"] == "sec-def"
    assert state == fold(read_topic_events(tmp_vault, *topic))


def test_current_snapshot_writes_nothing(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    snapshot = load_observer_snapshot(tmp_vault, *topic, write_back=False)

    assert snapshot.state == fold(read_topic_events(tmp_vault, *topic))
    state_dir = topic_directory(tmp_vault, *topic) / "state"
    assert not state_dir.exists()


def compacted(state_version: object, state: object) -> TopicEvent:
    payload = {"state_version": state_version, "state": state}
    return (
        "20260924-180000",
        Event(seq=3, t=0, origin="observer", kind=COMPACTED_EVENT_KIND, payload=payload),
    )


def test_a_compaction_of_a_newer_fold_or_a_bad_state_is_refused() -> None:
    with pytest.raises(InvalidEventError):
        fold([compacted(STATE_VERSION + 1, {})])
    with pytest.raises(InvalidEventError):
        fold([compacted(STATE_VERSION, {"sections": 3})])
    assert fold([compacted(STATE_VERSION, {})]) == fold([])
