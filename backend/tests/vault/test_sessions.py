"""Session lifecycle: start, resume, end, and the topic's `sessions` list kept in sync."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from studentassistant.vault import (
    NoOpenSessionError,
    SessionEndedError,
    SessionError,
    TopicNotFoundError,
    Vault,
    VaultError,
    create_subject,
    create_topic,
    end_session,
    get_topic,
    resume_session,
    sessions_directory,
    start_session,
)
from studentassistant.vault import sessions as sessions_module
from studentassistant.vault.files import read_yaml
from studentassistant.vault.session_models import SessionMeta
from studentassistant.vault.sessions import (
    EVENTS_FILE_NAME,
    SESSION_FILE_NAME,
    TRANSCRIPT_FILE_NAME,
)

HOST = "ubuntu-pc"
PROTOCOL = "1.0"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def test_starting_a_session_writes_its_directory_files_and_topic_entry(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    before = datetime.now(UTC)
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    after = datetime.now(UTC)

    assert re.fullmatch(r"\d{8}-\d{6}", session.id)
    directory = sessions_directory(tmp_vault, *topic) / session.id
    assert session.directory == directory
    assert sorted(entry.name for entry in directory.iterdir()) == sorted(
        [SESSION_FILE_NAME, TRANSCRIPT_FILE_NAME, EVENTS_FILE_NAME]
    )
    assert (directory / EVENTS_FILE_NAME).read_bytes() == b""
    assert (directory / TRANSCRIPT_FILE_NAME).read_bytes() == b""
    meta = read_yaml(directory / SESSION_FILE_NAME, SessionMeta)
    assert meta.id == session.id
    assert before <= meta.started_at <= after
    assert meta.ended_at is None
    assert meta.host == HOST
    assert meta.protocol_version == PROTOCOL
    assert get_topic(tmp_vault, *topic).topic.sessions == [session.id]


def test_ending_a_session_records_ended_at(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)

    meta = end_session(session)

    on_disk = read_yaml(session.directory / SESSION_FILE_NAME, SessionMeta)
    assert on_disk.ended_at is not None
    assert on_disk == meta == session.meta
    assert on_disk.ended_at >= on_disk.started_at


def test_resume_returns_the_open_session_and_refuses_once_it_ended(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)

    resumed = resume_session(tmp_vault, *topic)
    assert resumed.id == session.id
    assert resumed.meta == session.meta

    end_session(session)
    with pytest.raises(NoOpenSessionError):
        resume_session(tmp_vault, *topic)


def test_resume_of_a_topic_without_sessions_is_refused(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    with pytest.raises(NoOpenSessionError):
        resume_session(tmp_vault, *topic)


def test_two_sessions_started_in_the_same_second_get_distinct_ids(
    tmp_vault: Vault, topic: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = datetime(2026, 9, 24, 18, 30, 0, 250_000, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
            return frozen

    monkeypatch.setattr(sessions_module, "datetime", FrozenDatetime)
    first = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    second = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)

    assert first.id == "20260924-183000"
    assert second.id == "20260924-183001"
    assert get_topic(tmp_vault, *topic).topic.sessions == [first.id, second.id]


def test_resume_picks_the_latest_session_that_has_not_ended(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    first = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    second = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)

    assert resume_session(tmp_vault, *topic).id == second.id
    end_session(second)
    assert resume_session(tmp_vault, *topic).id == first.id


def test_a_session_cannot_end_twice_nor_take_lines_after_its_end(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host=HOST, protocol_version=PROTOCOL)
    end_session(session)

    with pytest.raises(SessionEndedError):
        end_session(session)
    with pytest.raises(SessionEndedError):
        session.append_event("marker", "user")
    with pytest.raises(SessionEndedError):
        session.append_transcript(0, 10, "hola")


def test_a_session_of_an_unknown_topic_is_refused_and_writes_nothing(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, _ = topic
    with pytest.raises(TopicNotFoundError):
        start_session(tmp_vault, subject, "no-existe", host=HOST, protocol_version=PROTOCOL)

    assert not sessions_directory(tmp_vault, subject, "no-existe").exists()


def test_session_errors_are_vault_errors() -> None:
    assert issubclass(SessionError, VaultError)
    assert issubclass(NoOpenSessionError, SessionError)
    assert issubclass(SessionEndedError, SessionError)
