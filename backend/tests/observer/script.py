"""The scripted two-session topic of the observer tests: see `conftest.py`."""

from __future__ import annotations

from typing import Any

from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    SEGMENT_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    TopicEvent,
)
from studentassistant.vault import Event
from studentassistant.vault.session_models import Origin

ScriptedEvent = tuple[Origin, str, dict[str, Any]]

FIRST_SESSION = "20260924-180000"
SECOND_SESSION = "20260925-180000"
CAPTURE_1 = "0b6f6c3e-8a6b-4c1e-9d59-3f1c2a4b5d6e"
CAPTURE_2 = "5a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def segment(segment_id: str) -> ScriptedEvent:
    return ("stt", SEGMENT_EVENT_KIND, {"segment_id": segment_id, "text": f"texto {segment_id}"})


def capture(capture_id: str) -> ScriptedEvent:
    return ("phone", CAPTURE_EVENT_KIND, {"capture_id": capture_id})


def op(op_name: str, /, origin: Origin = "observer", **fields: Any) -> ScriptedEvent:
    return (origin, STATE_OP_EVENT_KIND, {"op": op_name, **fields})


SCRIPT: list[tuple[str, list[ScriptedEvent]]] = [
    (
        FIRST_SESSION,
        [
            ("phone", "session.started", {}),
            op("set_source_context", kind="notes"),
            segment("s1-a"),
            segment("s1-b"),
            op("add_section", section_id="sec-def", title="Definición"),
            op("assign_segments", section_id="sec-def", segment_ids=["s1-a", "s1-b"]),
            capture(CAPTURE_1),
            op("link_capture", capture_id=CAPTURE_1, segment_ids=["s1-a"]),
            ("user", "marker", {"important": True}),
            segment("s1-c"),
            op(
                "add_concept",
                concept_id="c-limite",
                name="límite del cociente incremental",
                section_id="sec-def",
                segment_ids=["s1-c"],
            ),
            op(
                "add_pending",
                pending_id="p-1",
                category="illegible",
                description="Palabra ilegible junto a la fórmula",
                segment_ids=["s1-c"],
                capture_ids=[CAPTURE_1],
            ),
            op("link_capture", capture_id=CAPTURE_1, segment_ids=["s1-c", "s1-a"]),
            ("phone", "session.ended", {}),
        ],
    ),
    (
        SECOND_SESSION,
        [
            ("phone", "session.started", {}),
            op("set_source_context", kind="book", reference="Stewart, cap. 3, p. 112"),
            segment("s2-a"),
            op("add_section", section_id="sec-reglas", title="Reglas"),
            op(
                "add_section",
                section_id="sec-cadena",
                title="Regla de la cadena",
                parent_id="sec-reglas",
            ),
            op("assign_segments", section_id="sec-cadena", segment_ids=["s2-a"]),
            op("rename_section", section_id="sec-def", title="Definición de derivada"),
            op("resolve_pending", origin="user", pending_id="p-1", resolution="Dice «incremento»"),
            capture(CAPTURE_2),
            segment("s2-b"),
            op("assign_segments", section_id="sec-reglas", segment_ids=["s2-b", "s1-c"]),
            op(
                "add_pending",
                pending_id="p-2",
                category="unexplained_concept",
                description="Se menciona la regla de L'Hôpital sin explicarla",
                segment_ids=["s2-b"],
            ),
            op("note", text="El profesor insiste en la notación de Leibniz", segment_ids=["s2-a"]),
            ("phone", "session.ended", {}),
        ],
    ),
]


def to_topic_events(script: list[tuple[str, list[ScriptedEvent]]]) -> list[TopicEvent]:
    """The script as `(session_id, Event)` pairs, `seq` from 1 in each session, `t` rising."""
    return [
        (
            session_id,
            Event(seq=seq, t=seq * 1000, origin=origin, kind=kind, payload=payload),
        )
        for session_id, events in script
        for seq, (origin, kind, payload) in enumerate(events, start=1)
    ]
