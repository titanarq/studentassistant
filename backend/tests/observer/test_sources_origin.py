"""The `sources` origin (ADR-0003, #360): the observer's readers treat it as they treated the
sources module's events before, when they carried origin `observer`.

A `sources` state op (an illegible page's `add_pending`) is the backend's own, never a student's:
it is not shown as a `student op`, catch-up does not owe it, and a `resolve_pending` from it is
`auto_resolved`. An event log written before the change (origin `observer` on those events) folds,
batches and catches up exactly as the same log with origin `sources`.
"""

from __future__ import annotations

from typing import Any

import pytest

from studentassistant.observer import STATE_OP_EVENT_KIND, EventRef, TopicEvent, fold
from studentassistant.observer.catchup import ACK_EVENT_KIND, ack_payload, unanswered
from studentassistant.observer.context import PAGE_TRANSCRIPTION_KIND, batch_item
from studentassistant.vault import Event

from .script import CAPTURE_1, FIRST_SESSION, capture, op, segment, to_topic_events

NEXT_SESSION = "20260925-180000"
ILLEGIBLE = {"op": "add_pending", "pending_id": "p-1", "kind": "illegible", "text": "No se lee"}


def _line(origin: str) -> str | None:
    item = batch_item(STATE_OP_EVENT_KIND, origin, 4000, ILLEGIBLE)
    return None if item is None else item.line


# -- the three readers -- --------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["observer", "sources"])
def test_a_backend_state_op_is_not_a_student_op(origin: str) -> None:
    assert _line(origin) is None


@pytest.mark.parametrize("origin", ["user", "editor"])
def test_any_other_origin_is_still_a_student_op(origin: str) -> None:
    line = _line(origin)
    assert line is not None and line.startswith("student op [4.0 s]")


def test_catch_up_never_owes_a_sources_state_op() -> None:
    def event(seq: int, kind: str, origin: str, payload: dict[str, Any] | None = None) -> Event:
        return Event(seq=seq, t=seq * 1000, origin=origin, kind=kind, payload=payload or {})  # type: ignore[arg-type]

    note = {"op": "note", "text": "y", "segment_ids": []}

    events: list[TopicEvent] = [
        (FIRST_SESSION, event(1, "session.started", "user")),
        (FIRST_SESSION, event(2, ACK_EVENT_KIND, "observer", ack_payload(None))),
        (FIRST_SESSION, event(3, "capture.stored", "phone", {"capture_id": "c1"})),
        (FIRST_SESSION, event(4, STATE_OP_EVENT_KIND, "sources", ILLEGIBLE)),
        (
            FIRST_SESSION,
            event(5, PAGE_TRANSCRIPTION_KIND, "sources", {"capture_id": "c1", "text": "x"}),
        ),
        (FIRST_SESSION, event(6, STATE_OP_EVENT_KIND, "user", note)),
        (NEXT_SESSION, event(1, "session.started", "user")),
    ]
    found = unanswered(
        events, session_id=NEXT_SESSION, before=EventRef(session_id=NEXT_SESSION, seq=1)
    )
    # The capture, its transcription and the student's op; never the sources module's op.
    assert [(s, e.seq) for s, e in found.events] == [
        (FIRST_SESSION, 3),
        (FIRST_SESSION, 5),
        (FIRST_SESSION, 6),
    ]


@pytest.mark.parametrize(
    ("origin", "expected"),
    [("observer", "auto_resolved"), ("sources", "auto_resolved"), ("user", "resolved")],
)
def test_a_resolution_without_status_from_the_backend_is_automatic(
    origin: Any, expected: str
) -> None:
    state = fold(
        to_topic_events(
            [
                (
                    FIRST_SESSION,
                    [
                        capture(CAPTURE_1),
                        op("add_pending", origin="sources", **_pending_fields()),
                        op("resolve_pending", origin=origin, pending_id="p-1", resolution="Ya"),
                    ],
                )
            ]
        )
    )
    assert state.pending["p-1"].status == expected
    assert state.pending["p-1"].created_by == "sources"


def _pending_fields() -> dict[str, Any]:
    fields = {k: v for k, v in ILLEGIBLE.items() if k != "op"}
    return {**fields, "capture_ids": [CAPTURE_1]}


# -- an event log written before the change -- -----------------------------------------------------


def _log(origin: Any) -> list[TopicEvent]:
    """A transcription session: the sources module's events carry `origin`."""
    return to_topic_events(
        [
            (
                FIRST_SESSION,
                [
                    ("user", "session.started", {}),
                    segment("s-1"),
                    capture(CAPTURE_1),
                    (origin, "capture.triaged", {"capture_id": CAPTURE_1, "status": "kept"}),
                    op("add_pending", origin=origin, **_pending_fields()),
                    (origin, PAGE_TRANSCRIPTION_KIND, {"capture_id": CAPTURE_1, "text": "# A"}),
                    op("resolve_pending", origin=origin, pending_id="p-1", resolution="Se lee"),
                    op("note", text="nota", segment_ids=["s-1"]),
                    (
                        "observer",
                        ACK_EVENT_KIND,
                        ack_payload(EventRef(session_id=FIRST_SESSION, seq=2)),
                    ),
                    segment("s-2"),
                    ("user", "session.ended", {}),
                ],
            ),
            (NEXT_SESSION, [("user", "session.started", {})]),
        ]
    )


def _batches(events: list[TopicEvent]) -> list[str]:
    items = (batch_item(e.kind, e.origin, e.t, e.payload) for _, e in events)
    return [item.line for item in items if item is not None]


def test_an_old_log_folds_batches_and_catches_up_as_a_new_one() -> None:
    old, new = _log("observer"), _log("sources")

    old_state, new_state = fold(old), fold(new)
    assert old_state.pending["p-1"].status == "auto_resolved"
    assert old_state.pending["p-1"].created_by == "observer"  # as it was written
    assert new_state.pending["p-1"].created_by == "sources"
    new_state.pending["p-1"].created_by = "observer"
    assert new_state == old_state

    assert _batches(old) == _batches(new)
    assert not any(line.startswith("student op") for line in _batches(old))

    before = EventRef(session_id=NEXT_SESSION, seq=1)
    old_owed = unanswered(old, session_id=NEXT_SESSION, before=before)
    new_owed = unanswered(new, session_id=NEXT_SESSION, before=before)
    assert [(s, e.seq) for s, e in old_owed.events] == [(s, e.seq) for s, e in new_owed.events]
    assert [e.kind for _, e in old_owed.events] == [
        "capture.stored",
        PAGE_TRANSCRIPTION_KIND,
        "transcript.final",
    ]
