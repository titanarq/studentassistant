"""Appending to a session: the store numbers each log, and a resumed session keeps counting."""

from __future__ import annotations

import pytest
from secret_samples import GITHUB_TOKEN

from studentassistant.vault import (
    SecretRefused,
    Vault,
    create_subject,
    create_topic,
    end_session,
    resume_session,
    start_session,
)
from studentassistant.vault.session_models import Event, TranscriptSegment, TranscriptWord


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Física").slug
    return subject, create_topic(tmp_vault, subject, "Cinemática").slug


def test_a_mixed_sequence_numbers_each_log_from_one_without_gaps(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")

    session.append_event("session_started", "phone")
    session.append_transcript(0, 1200, "hoy vemos cinemática")
    session.append_event("capture_stored", "phone", {"capture_id": "c1"})
    session.append_transcript(
        1200, 2000, "velocidad", words=[TranscriptWord(text="velocidad", t_start=1200, t_end=2000)]
    )
    session.append_event("marker", "user", t=5000)
    session.append_event("segment_final", "stt", {"seq": 2})

    events = list(session.read_events())
    segments = list(session.read_transcript())
    assert [event.seq for event in events] == [1, 2, 3, 4]
    assert [segment.seq for segment in segments] == [1, 2]
    times = [event.t for event in events]
    assert times == sorted(times)
    assert events[2].t == 5000
    assert events[1].payload == {"capture_id": "c1"}
    assert segments[1].words == [TranscriptWord(text="velocidad", t_start=1200, t_end=2000)]


def test_append_returns_what_was_written(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")

    event = session.append_event("marker", "user", {"importante": True})
    segment = session.append_transcript(0, 10, "hola")

    assert list(session.read_events()) == [event]
    assert list(session.read_transcript()) == [segment]
    assert isinstance(event, Event) and isinstance(segment, TranscriptSegment)


def test_a_resumed_session_continues_each_log_instead_of_restarting(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")
    for _ in range(3):
        session.append_event("marker", "user")
    session.append_transcript(0, 10, "uno")
    last_t = session.append_event("marker", "user", t=90_000).t

    resumed = resume_session(tmp_vault, *topic)
    event = resumed.append_event("marker", "user")
    segment = resumed.append_transcript(10, 20, "dos")

    assert event.seq == 5
    assert event.t >= last_t
    assert segment.seq == 2
    assert [e.seq for e in resumed.read_events()] == [1, 2, 3, 4, 5]


def test_a_refused_secret_leaves_the_log_and_the_next_seq_unchanged(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")
    session.append_event("marker", "user")
    before = session.events_path.read_bytes()
    transcript_before = session.transcript_path.read_bytes()

    with pytest.raises(SecretRefused):
        session.append_event("note", "user", {"texto": f"token {GITHUB_TOKEN}"})
    with pytest.raises(SecretRefused):
        session.append_transcript(0, 10, f"mi token es {GITHUB_TOKEN}")

    assert session.events_path.read_bytes() == before
    assert session.transcript_path.read_bytes() == transcript_before
    assert session.append_event("marker", "user").seq == 2
    assert session.append_transcript(0, 10, "hola").seq == 1


def test_a_bad_origin_is_refused_and_appends_nothing(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    session = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")

    with pytest.raises(ValueError):
        session.append_event("marker", "server")  # type: ignore[arg-type]

    assert session.events_path.read_bytes() == b""
    assert session.append_event("marker", "user").seq == 1


def test_an_ended_and_resumed_topic_gets_a_fresh_session_numbered_from_one(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    first = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")
    first.append_event("marker", "user")
    end_session(first)

    second = start_session(tmp_vault, *topic, host="pc", protocol_version="1.0")

    assert second.append_event("marker", "user").seq == 1
