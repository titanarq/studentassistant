"""Topic-wide reading: `list_sessions` and `read_topic_events` across the sessions of a topic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studentassistant.vault import (
    Event,
    SessionFileError,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    end_session,
    list_sessions,
    read_topic_events,
    start_session,
)
from studentassistant.vault import sessions as sessions_module
from studentassistant.vault.sessions import EVENTS_FILE_NAME

HOST = "ubuntu-pc"
PROTOCOL = "1.0"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Session start times the next `start_session` calls take, one per call."""
    times: list[datetime] = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
            return times.pop(0) if times else datetime(2026, 9, 24, 20, 0, tzinfo=UTC)

    monkeypatch.setattr(sessions_module, "datetime", FrozenDatetime)
    return times


def test_list_sessions_returns_open_and_ended_sessions_in_id_order(
    tmp_vault: Vault, topic: tuple[str, str], frozen_clock: list[datetime]
) -> None:
    frozen_clock.extend(
        [
            datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
            datetime(2026, 9, 24, 19, 0, tzinfo=UTC),
        ]
    )
    first = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    end_session(first, ended_at=datetime(2026, 9, 24, 18, 30, tzinfo=UTC))
    second = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)

    sessions = list_sessions(tmp_vault, *topic)

    assert [meta.id for meta in sessions] == ["20260924-180000", "20260924-190000"]
    assert sessions[0].ended_at is not None
    assert sessions[1].ended_at is None
    assert sessions[1] == second.meta


def test_list_sessions_of_a_topic_without_sessions_is_empty(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    assert list_sessions(tmp_vault, *topic) == []


def test_list_sessions_writes_nothing(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    before = {p: p.read_bytes() for p in tmp_vault.path.rglob("*") if p.is_file()}

    list_sessions(tmp_vault, *topic)
    list(read_topic_events(tmp_vault, *topic))

    after = {p: p.read_bytes() for p in tmp_vault.path.rglob("*") if p.is_file()}
    assert after == before


def test_a_missing_topic_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, _ = topic
    with pytest.raises(TopicNotFoundError):
        list_sessions(tmp_vault, subject, "integrales")
    with pytest.raises(TopicNotFoundError):
        list(read_topic_events(tmp_vault, subject, "integrales"))


def test_read_topic_events_orders_sessions_by_id_and_events_by_seq(
    tmp_vault: Vault, topic: tuple[str, str], frozen_clock: list[datetime]
) -> None:
    frozen_clock.extend(
        [
            datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
            datetime(2026, 9, 24, 19, 0, tzinfo=UTC),
        ]
    )
    first = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    second = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    # Interleave the appends, so file order alone would not give session order.
    first.append_event("capture.received", "phone", {"capture_id": "a"}, t=10)
    second.append_event("capture.received", "phone", {"capture_id": "c"}, t=5)
    first.append_event("page.transcribed", "observer", {"page": 1}, t=20)
    end_session(first)
    second.append_event("page.transcribed", "observer", {"page": 2}, t=15)

    pairs = list(read_topic_events(tmp_vault, *topic))

    assert [(session_id, event.seq) for session_id, event in pairs] == [
        (first.id, 1),
        (first.id, 2),
        (second.id, 1),
        (second.id, 2),
    ]
    assert all(isinstance(event, Event) for _, event in pairs)
    assert pairs[0][1].payload == {"capture_id": "a"}
    assert pairs[3][1].kind == "page.transcribed"


def test_read_topic_events_sorts_a_union_merged_log_by_seq(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    for n in range(3):
        session.append_event("note", "user", {"n": n}, t=n)
    lines = session.events_path.read_bytes().splitlines(keepends=True)
    session.events_path.write_bytes(b"".join(reversed(lines)))

    seqs = [event.seq for _, event in read_topic_events(tmp_vault, *topic)]

    assert seqs == [1, 2, 3]


def test_read_topic_events_leaves_out_a_torn_last_line(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    session.append_event("note", "user", {"n": 1}, t=1)
    with session.events_path.open("ab") as log:
        log.write(b'{"seq": 2, "t": 2, "ori')

    assert [event.seq for _, event in read_topic_events(tmp_vault, *topic)] == [1]


def test_a_listed_session_without_an_event_log_is_refused(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    (session.directory / EVENTS_FILE_NAME).unlink()

    with pytest.raises(SessionFileError):
        list(read_topic_events(tmp_vault, *topic))
