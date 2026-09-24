"""State-op models: each op kind parses from its event payload and a malformed one is refused."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from studentassistant.observer import (
    OP_NAMES,
    AddConcept,
    AddPending,
    AddSection,
    AssignSegments,
    LinkCapture,
    Note,
    RenameSection,
    ResolvePending,
    SetSourceContext,
    op_payload,
    parse_op,
)

EXAMPLES: list[tuple[dict[str, Any], type]] = [
    ({"op": "add_section", "section_id": "s", "title": "Definición"}, AddSection),
    ({"op": "rename_section", "section_id": "s", "title": "Otra"}, RenameSection),
    ({"op": "assign_segments", "section_id": "s", "segment_ids": ["a", "b"]}, AssignSegments),
    ({"op": "add_concept", "concept_id": "c", "name": "límite"}, AddConcept),
    ({"op": "link_capture", "capture_id": "k", "segment_ids": ["a"]}, LinkCapture),
    ({"op": "set_source_context", "kind": "pdf", "reference": "tema3.pdf"}, SetSourceContext),
    (
        {"op": "add_pending", "pending_id": "p", "category": "contradiction", "description": "x"},
        AddPending,
    ),
    ({"op": "resolve_pending", "pending_id": "p", "resolution": "ok"}, ResolvePending),
    ({"op": "note", "text": "algo"}, Note),
]


def test_every_op_name_has_an_example() -> None:
    assert sorted(payload["op"] for payload, _ in EXAMPLES) == sorted(OP_NAMES)


@pytest.mark.parametrize(("payload", "model"), EXAMPLES, ids=[p["op"] for p, _ in EXAMPLES])
def test_each_op_parses_into_its_model_and_round_trips(
    payload: dict[str, Any], model: type
) -> None:
    op = parse_op(payload)
    assert isinstance(op, model)
    assert parse_op(op_payload(op)) == op


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "delete_everything"},
        {"section_id": "s", "title": "sin op"},
        {"op": "add_section", "title": "sin id"},
        {"op": "add_section", "section_id": "", "title": "id vacío"},
        {"op": "add_section", "section_id": "s", "title": "x", "colour": "rojo"},
        {"op": "assign_segments", "section_id": "s", "segment_ids": []},
        {"op": "set_source_context", "kind": "video"},
        {"op": "add_pending", "pending_id": "p", "category": "boring", "description": "x"},
    ],
)
def test_a_malformed_op_is_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_op(payload)
