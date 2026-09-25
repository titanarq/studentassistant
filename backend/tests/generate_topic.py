"""A fixture topic for the notes generation tests: "Derivadas", one ended session, two pages.

- `sources/notes/page-001.jpg` (+ its cropped `page-001.page.jpg`) with a plain transcription
  `page-001.md`, so no image is needed;
- `sources/notes/page-002.jpg` with no transcription, so the editor must see its image;
- one session with three final segments (two in the section "Definición", one unassigned), a
  capture of page 1 linked to the first segment, a concept and an open and a resolved pending item;
- the subject's style guide.

`valid_notes()` is an answer that passes the validator against this topic; `invalid_notes()` one
that does not (a paragraph without provenance).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf

from studentassistant.observer import CAPTURE_EVENT_KIND, SEGMENT_EVENT_KIND, STATE_OP_EVENT_KIND
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    sources_directory,
    start_session,
)

STYLE_GUIDE = "Pon las definiciones en negrita."
PAGE_1_TRANSCRIPTION = "# Derivadas\n\nDerivada: límite del cociente incremental.\n"
PAGE_1_IMAGE = b"\xff\xd8\xff\xe0 page one still"
PAGE_1_CROP = b"\xff\xd8\xff\xe0 page one cropped"
PAGE_2_IMAGE = b"\xff\xd8\xff\xe0 page two still"
CAPTURE_ID = "0b6f6c3e-8a6b-4c1e-9d59-3f1c2a4b5d6e"


@dataclass(frozen=True)
class GenerateTopic:
    vault: Vault
    subject: str
    topic: str
    session: str


def _op(op_name: str, /, **fields: Any) -> dict[str, Any]:
    return {"op": op_name, **fields}


def make_topic(vault: Vault) -> GenerateTopic:
    subject = create_subject(vault, "Matemáticas", style_guide=STYLE_GUIDE).slug
    topic = create_topic(vault, subject, "Derivadas").slug
    session = start_session(vault, subject, topic, host="pc", protocol_version="1.1")

    page_1 = put_source(
        vault,
        subject,
        topic,
        "notes",
        "page.jpg",
        PAGE_1_IMAGE,
        {"capture_id": CAPTURE_ID, "session": session.id},
        derived={"page.jpg": PAGE_1_CROP},
    )
    # The page transcription (#50) is `page-NNN.md` beside the page; written here by hand.
    (sources_directory(vault, subject, topic, "notes") / "page-001.md").write_text(
        PAGE_1_TRANSCRIPTION, encoding="utf-8"
    )
    put_source(vault, subject, topic, "notes", "page.jpg", PAGE_2_IMAGE, {"session": session.id})

    def segment(segment_id: str, start_ms: int, end_ms: int, text: str) -> None:
        session.append_transcript(start_ms, end_ms, text)
        session.append_event(
            SEGMENT_EVENT_KIND,
            "stt",
            {
                "segment_id": segment_id,
                "session_start_ms": start_ms,
                "session_end_ms": end_ms,
                "text": text,
                "language": "es",
            },
        )

    def op(op_name: str, /, origin: str = "observer", **fields: Any) -> None:
        session.append_event(STATE_OP_EVENT_KIND, origin, _op(op_name, **fields))  # type: ignore[arg-type]

    session.append_event("session.started", "user", {})
    segment("s-1", 2_000, 9_500, "La derivada es el límite del cociente incremental.")
    session.append_event(
        CAPTURE_EVENT_KIND,
        "phone",
        {"capture_id": CAPTURE_ID, "source_path": page_1.relative_to(vault.path).as_posix()},
    )
    op("add_section", section_id="sec-def", title="Definición")
    op("assign_segments", section_id="sec-def", segment_ids=["s-1"])
    op("link_capture", capture_id=CAPTURE_ID, segment_ids=["s-1"])
    segment("s-2", 10_000, 14_200, "Se escribe f prima de x.")
    op("assign_segments", section_id="sec-def", segment_ids=["s-2"])
    op(
        "add_concept",
        concept_id="c-derivada",
        name="derivada",
        section_id="sec-def",
        segment_ids=["s-1"],
    )
    segment("s-3", 3_725_000, 3_730_000, "Mañana repasamos la regla de la cadena.")
    op(
        "add_pending",
        pending_id="p-1",
        kind="unexplained_concept",
        text="Se menciona la regla de la cadena sin explicarla.",
        segment_ids=["s-3"],
    )
    # One op in the pre-#178 shape (`category`/`description`), which the fold still reads.
    op(
        "add_pending",
        pending_id="p-2",
        category="illegible",
        description="Palabra ilegible en la página 1.",
        capture_ids=[CAPTURE_ID],
    )
    op("resolve_pending", origin="user", pending_id="p-2", resolution="Pone «incremental».")
    op(
        "add_pending",
        pending_id="p-3",
        kind="possible_error",
        text="¿Falta el signo en la fórmula?",
        source_refs=["sources/notes/page-002.jpg"],
    )
    op("resolve_pending", origin="user", pending_id="p-3", status="dismissed")
    session.append_event("session.ended", "user", {})
    end_session(session)
    return GenerateTopic(vault=vault, subject=subject, topic=topic, session=session.id)


def valid_notes(session: str) -> str:
    return (
        "# Derivadas\n"
        "\n"
        "## 1. Definición {#definicion}\n"
        "\n"
        "**Derivada**: el límite del cociente incremental.[^p1][^t1]\n"
        "\n"
        "Se escribe $f'(x)$.[^t2]\n"
        "\n"
        "## 2. Próximo día {#proximo-dia}\n"
        "\n"
        "- La regla de la cadena, que se verá mañana.[^t3][^p2]\n"
        "\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^p2]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
        f"[^t1]: [Transcripción, 00:00:02–00:00:10](../sessions/{session}/transcript.jsonl"
        "#t=00:00:02-00:00:10)\n"
        f"[^t2]: [Transcripción, 00:00:10–00:00:15](../sessions/{session}/transcript.jsonl"
        "#t=00:00:10-00:00:15)\n"
        f"[^t3]: [Transcripción, 01:02:05–01:02:10](../sessions/{session}/transcript.jsonl"
        "#t=01:02:05-01:02:10)\n"
    )


def invalid_notes(session: str) -> str:
    """`valid_notes` with one paragraph that cites nothing."""
    return valid_notes(session).replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.")


def make_pdf(pages: int) -> bytes:
    """A PDF whose page `n` reads «Página n del libro»."""
    document = pymupdf.open()
    try:
        for number in range(1, pages + 1):
            document.new_page().insert_text((72, 72), f"Página {number} del libro", fontsize=14)
        return document.tobytes()
    finally:
        document.close()
