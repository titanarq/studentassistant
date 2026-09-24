"""`fold`: the expected state of the scripted topic, determinism, ignored kinds, unknown ids."""

from __future__ import annotations

import pytest

from studentassistant.observer import (
    STATE_OP_EVENT_KIND,
    AddSection,
    DuplicateIdError,
    EventOrderError,
    EventRef,
    InvalidEventError,
    PendingAlreadyResolvedError,
    TopicEvent,
    TopicState,
    UnknownIdError,
    apply_op,
    fold,
    validate_op,
)
from studentassistant.vault import Event

from .script import CAPTURE_1, CAPTURE_2, FIRST_SESSION, SECOND_SESSION, ScriptedEvent, op, segment


def events_of(*scripted: ScriptedEvent) -> list[TopicEvent]:
    return [
        (FIRST_SESSION, Event(seq=seq, t=seq, origin=origin, kind=kind, payload=payload))
        for seq, (origin, kind, payload) in enumerate(scripted, start=1)
    ]


def test_fold_of_the_scripted_topic(topic_events: list[TopicEvent]) -> None:
    state = fold(topic_events)

    assert [s.id for s in state.outline()] == ["sec-def", "sec-reglas"]
    assert [s.id for s in state.outline("sec-reglas")] == ["sec-cadena"]
    assert state.sections["sec-def"].title == "Definición de derivada"
    assert state.sections["sec-def"].added_at == EventRef(session_id=FIRST_SESSION, seq=5)
    assert list(state.segments) == ["s1-a", "s1-b", "s1-c", "s2-a", "s2-b"]
    assert list(state.captures) == [CAPTURE_1, CAPTURE_2]
    assert state.assignments == {
        "s1-a": "sec-def",
        "s1-b": "sec-def",
        "s2-a": "sec-cadena",
        "s2-b": "sec-reglas",
        "s1-c": "sec-reglas",
    }
    assert state.segments_of("sec-reglas") == ["s1-c", "s2-b"]
    assert state.concepts["c-limite"].segment_ids == ["s1-c"]
    assert state.capture_links == {CAPTURE_1: ["s1-a", "s1-c"]}
    assert state.source_context is not None
    assert state.source_context.kind == "book"
    assert state.source_context.reference == "Stewart, cap. 3, p. 112"
    assert [p.id for p in state.open_pending()] == ["p-2"]
    resolved = state.resolved_pending()
    assert [p.id for p in resolved] == ["p-1"]
    assert resolved[0].resolution == "Dice «incremento»"
    assert resolved[0].resolved_at == EventRef(session_id=SECOND_SESSION, seq=8)
    assert [n.text for n in state.notes] == ["El profesor insiste en la notación de Leibniz"]


def test_fold_is_deterministic(topic_events: list[TopicEvent]) -> None:
    first = fold(topic_events)
    second = fold(list(topic_events))
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert TopicState.model_validate_json(first.model_dump_json()) == first


def test_fold_does_not_mutate_its_input(topic_events: list[TopicEvent]) -> None:
    before = [(sid, event.model_copy(deep=True)) for sid, event in topic_events]
    fold(topic_events)
    assert topic_events == before


def test_events_of_other_kinds_are_ignored(topic_events: list[TopicEvent]) -> None:
    relevant = [
        (sid, e)
        for sid, e in topic_events
        if e.kind in (STATE_OP_EVENT_KIND, "transcript.final", "capture.stored")
    ]
    assert len(relevant) < len(topic_events)
    assert fold(relevant) == fold(topic_events)
    assert fold(events_of(("user", "marker", {"op": "add_section"}))) == TopicState()


@pytest.mark.parametrize(
    ("scripted", "entity", "missing"),
    [
        ([op("rename_section", section_id="nope", title="x")], "section", "nope"),
        ([op("add_section", section_id="a", title="x", parent_id="nope")], "section", "nope"),
        (
            [
                op("add_section", section_id="a", title="x"),
                op("assign_segments", section_id="a", segment_ids=["ghost"]),
            ],
            "segment",
            "ghost",
        ),
        (
            [segment("s"), op("assign_segments", section_id="nope", segment_ids=["s"])],
            "section",
            "nope",
        ),
        (
            [segment("s"), op("link_capture", capture_id="ghost", segment_ids=["s"])],
            "capture",
            "ghost",
        ),
        ([op("add_concept", concept_id="c", name="x", section_id="nope")], "section", "nope"),
        (
            [
                op(
                    "add_pending",
                    pending_id="p",
                    category="incomplete",
                    description="x",
                    capture_ids=["ghost"],
                )
            ],
            "capture",
            "ghost",
        ),
        ([op("resolve_pending", pending_id="ghost", resolution="x")], "pending item", "ghost"),
        ([op("note", text="x", segment_ids=["ghost"])], "segment", "ghost"),
    ],
)
def test_an_op_with_an_unknown_id_raises(
    scripted: list[ScriptedEvent], entity: str, missing: str
) -> None:
    with pytest.raises(UnknownIdError) as caught:
        fold(events_of(*scripted))
    assert caught.value.entity == entity
    assert caught.value.id == missing
    assert caught.value.at == EventRef(session_id=FIRST_SESSION, seq=len(scripted))


def test_validate_op_rejects_before_applying() -> None:
    state = TopicState()
    bad = AddSection(section_id="a", title="x", parent_id="nope")
    with pytest.raises(UnknownIdError):
        validate_op(state, bad)
    with pytest.raises(UnknownIdError):
        apply_op(state, bad, EventRef(session_id=FIRST_SESSION, seq=1))
    assert state == TopicState()


def test_apply_op_leaves_its_input_unchanged() -> None:
    state = TopicState()
    new = apply_op(state, AddSection(section_id="a", title="x"), EventRef(session_id="s", seq=1))
    assert state == TopicState()
    assert list(new.sections) == ["a"]


def test_duplicate_ids_raise() -> None:
    with pytest.raises(DuplicateIdError):
        fold(
            events_of(
                op("add_section", section_id="a", title="x"),
                op("add_section", section_id="a", title="y"),
            )
        )
    with pytest.raises(DuplicateIdError):
        fold(
            events_of(
                op("add_concept", concept_id="c", name="x"),
                op("add_concept", concept_id="c", name="y"),
            )
        )


def test_resolving_twice_raises() -> None:
    scripted = [
        op("add_pending", pending_id="p", category="incomplete", description="x"),
        op("resolve_pending", pending_id="p", resolution="a"),
        op("resolve_pending", pending_id="p", resolution="b"),
    ]
    with pytest.raises(PendingAlreadyResolvedError):
        fold(events_of(*scripted))


def test_a_malformed_op_event_raises() -> None:
    with pytest.raises(InvalidEventError):
        fold(events_of(("observer", STATE_OP_EVENT_KIND, {"op": "delete_everything"})))
    with pytest.raises(InvalidEventError):
        fold(events_of(("stt", "transcript.final", {"text": "sin id"})))


def test_a_repeated_segment_event_keeps_the_first_registration() -> None:
    state = fold(events_of(segment("s"), segment("s")))
    assert state.segments == {"s": EventRef(session_id=FIRST_SESSION, seq=1)}


def test_out_of_order_events_raise(topic_events: list[TopicEvent]) -> None:
    with pytest.raises(EventOrderError):
        fold(list(reversed(topic_events)))
