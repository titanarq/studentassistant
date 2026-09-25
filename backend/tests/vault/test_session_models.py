"""Session and event models: the session id pattern, and how lenient a log line reader is."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from studentassistant.vault.session_models import (
    EVENT_SCHEMA_VERSION,
    Event,
    SessionMeta,
    TranscriptSegment,
)

STARTED = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize("bad_id", ["2026-09-24", "20260924183000", "20260924-1830", "x"])
def test_a_malformed_session_id_is_refused(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        SessionMeta(id=bad_id, started_at=STARTED, host="pc", protocol_version="1.0")


def test_a_well_formed_session_starts_without_an_end() -> None:
    meta = SessionMeta(id="20260924-183000", started_at=STARTED, host="pc", protocol_version="1.0")

    assert meta.ended_at is None
    # A session.yaml written before `kind` existed is a study session.
    assert meta.kind == "study" and meta.is_study


def test_a_review_session_is_not_a_study_session_and_an_unknown_kind_is_refused() -> None:
    meta = SessionMeta(
        id="20260924-183000", started_at=STARTED, host="pc", protocol_version="1.0", kind="review"
    )
    assert not meta.is_study
    with pytest.raises(ValidationError):
        SessionMeta(
            id="20260924-183000",
            started_at=STARTED,
            host="pc",
            protocol_version="1.0",
            kind="exam",  # type: ignore[arg-type]
        )


def test_an_origin_outside_the_five_is_refused() -> None:
    with pytest.raises(ValidationError):
        Event(seq=1, t=0, origin="server", kind="marker")  # type: ignore[arg-type]


def test_the_payload_round_trips_unchanged() -> None:
    payload = {"texto": "límite", "nested": {"a": [1, 2.5, None, True]}, "empty": {}}
    event = Event(seq=3, t=1200, origin="observer", kind="state_op", payload=payload)

    back = Event.model_validate_json(event.model_dump_json())

    assert back.payload == payload
    assert back == event


def test_an_unknown_kind_and_an_older_schema_version_still_parse() -> None:
    line = {
        "seq": 7,
        "t": 5000,
        "origin": "phone",
        "kind": "kind_from_the_future",
        "schema_version": EVENT_SCHEMA_VERSION - 1,
        "payload": {"x": 1},
        "added_by_a_newer_backend": True,
    }

    event = Event.model_validate(line)

    assert event.kind == "kind_from_the_future"
    assert event.schema_version == EVENT_SCHEMA_VERSION - 1


def test_a_segment_without_words_parses_and_one_with_words_keeps_them() -> None:
    bare = TranscriptSegment(seq=1, t_start=0, t_end=900, text="hola")
    worded = TranscriptSegment.model_validate(
        {
            "seq": 2,
            "t_start": 900,
            "t_end": 1500,
            "text": "qué tal",
            "words": [{"text": "qué", "t_start": 900, "t_end": 1100}],
        }
    )

    assert bare.words is None
    assert worded.words is not None and worded.words[0].text == "qué"
