"""A fixture topic for the doubts resolution tests: `generate_topic`'s "Derivadas" with notes.

On top of `make_topic` (open `p-1`, an unexplained concept; `p-2` resolved, `p-3` dismissed) a
second ended session adds `p-4` (an illegible word on page 1) and `p-5` (a contradiction between
pages 1 and 2), and `valid_notes` is written as `notes/apuntes.md` (committed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from generate_topic import CAPTURE_ID, GenerateTopic, make_topic, valid_notes
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.vault import GitSync, Vault, end_session, start_session, write_notes


@dataclass(frozen=True)
class DoubtsTopic(GenerateTopic):
    second_session: str = ""

    @property
    def notes(self) -> str:
        return valid_notes(self.session)


def make_doubts_topic(vault: Vault, *, with_notes: bool = True) -> DoubtsTopic:
    base = make_topic(vault)
    session = start_session(vault, base.subject, base.topic, host="pc", protocol_version="1.1")

    def op(op_name: str, **fields: Any) -> None:
        session.append_event(STATE_OP_EVENT_KIND, "observer", {"op": op_name, **fields})

    session.append_event("session.started", "user", {})
    session.append_event("capture.stored", "phone", {"capture_id": "cap-2"})
    op(
        "add_pending",
        pending_id="p-4",
        kind="illegible",
        text="Palabra ilegible tras «cociente» en la página 1.",
        capture_ids=[CAPTURE_ID],
    )
    op(
        "add_pending",
        pending_id="p-5",
        kind="contradiction",
        text="La página 1 y la página 2 dan años distintos.",
        source_refs=["sources/notes/page-001.jpg", "sources/notes/page-002.jpg"],
    )
    session.append_event("session.ended", "user", {})
    end_session(session)
    if with_notes:
        write_notes(vault, base.subject, base.topic, valid_notes(base.session))
    GitSync(vault).checkpoint("fixture")
    return DoubtsTopic(
        vault=vault,
        subject=base.subject,
        topic=base.topic,
        session=base.session,
        second_session=session.id,
    )


def transcript_id(topic: GenerateTopic, span: str = "01:02:05-01:02:10") -> str:
    return f"sessions/{topic.session}#t={span}"


def review_reply(topic: GenerateTopic) -> dict[str, Any]:
    """A valid `resolve_doubts` input: p-1 auto-resolved from the transcript, p-4 and p-5 asked."""
    return {
        "decisions": [
            {
                "pending_id": "p-1",
                "action": "auto_resolve",
                "resolution": "La regla de la cadena se explica en la transcripción.",
                "evidence": [
                    {"source_id": transcript_id(topic), "quote": "Mañana repasamos la regla"}
                ],
                "edits": [
                    {
                        "op": "replace_block",
                        "section": "proximo-dia",
                        "block": 1,
                        "text": "- La regla de la cadena, que se repasa mañana.[^t3][^p2]",
                    }
                ],
            },
            {
                "pending_id": "p-4",
                "action": "ask",
                "question": "¿Qué pone después de «cociente» en la página 1?",
                "suggestions": ["incremental", "diferencial"],
            },
            {
                "pending_id": "p-5",
                "action": "ask",
                "question": "¿Qué año es el correcto?",
                "options": [
                    {"source_id": "sources/notes/page-001.jpg", "says": "1789"},
                    {"source_id": "sources/notes/page-002.jpg", "says": "1791"},
                ],
            },
        ]
    }
