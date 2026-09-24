"""Reading a session's transcript without a handle: open or ended, nothing written."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.vault import (
    SessionError,
    SessionFileError,
    SessionNotFoundError,
    TopicNotFoundError,
    TranscriptSegment,
    Vault,
    create_subject,
    create_topic,
    end_session,
    read_session_transcript,
    start_session,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Física").slug
    return subject, create_topic(tmp_vault, subject, "Cinemática").slug


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_an_ended_session_is_readable_and_nothing_changes(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    session.append_transcript(0, 900, "la velocidad")
    session.append_transcript(
        900, 1800, "es la derivada", words=[{"text": "es", "t_start": 900, "t_end": 1000}]
    )
    end_session(session)
    before = _snapshot(tmp_vault.path)

    segments = read_session_transcript(tmp_vault, *topic, session.id)

    assert [s.text for s in segments] == ["la velocidad", "es la derivada"]
    assert all(isinstance(s, TranscriptSegment) for s in segments)
    assert [s.seq for s in segments] == [1, 2]
    assert _snapshot(tmp_vault.path) == before


def test_an_open_session_is_readable_and_its_handle_keeps_appending(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    assert read_session_transcript(tmp_vault, *topic, session.id) == []
    session.append_transcript(0, 100, "uno")
    assert [s.seq for s in read_session_transcript(tmp_vault, *topic, session.id)] == [1]
    assert session.append_transcript(100, 200, "dos").seq == 2


def test_a_torn_last_line_is_left_out_and_lines_are_sorted_by_seq(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    session.append_transcript(0, 100, "uno")
    session.append_transcript(100, 200, "dos")
    lines = session.transcript_path.read_bytes().splitlines(keepends=True)
    session.transcript_path.write_bytes(lines[1] + lines[0] + b'{"seq":3,"t_st')
    before = session.transcript_path.read_bytes()

    segments = read_session_transcript(tmp_vault, *topic, session.id)

    assert [s.text for s in segments] == ["uno", "dos"]
    assert session.transcript_path.read_bytes() == before


@pytest.mark.parametrize(
    "session_id", ["20260924-000000", "../20260924-000000", "..", "", "20260924-000000/x"]
)
def test_an_unknown_session_is_a_session_not_found_error(
    tmp_vault: Vault, topic: tuple[str, str], session_id: str
) -> None:
    start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    with pytest.raises(SessionNotFoundError):
        read_session_transcript(tmp_vault, *topic, session_id)
    assert issubclass(SessionNotFoundError, SessionError)


def test_a_session_of_another_topic_is_not_found(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    other = create_topic(tmp_vault, topic[0], "Dinámica").slug
    session = start_session(tmp_vault, topic[0], other, host="pc", protocol_version="1")
    with pytest.raises(SessionNotFoundError):
        read_session_transcript(tmp_vault, *topic, session.id)


def test_a_listed_session_without_transcript_is_a_session_file_error(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    session.transcript_path.unlink()
    with pytest.raises(SessionFileError):
        read_session_transcript(tmp_vault, *topic, session.id)


def test_a_topic_slug_that_is_not_a_slug_is_not_found(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1")
    with pytest.raises(TopicNotFoundError):
        read_session_transcript(tmp_vault, topic[0], "../cinematica", session.id)
