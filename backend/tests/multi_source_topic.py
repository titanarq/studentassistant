"""A fixture topic with every source kind, two of which disagree: "La máquina de vapor".

- `sources/notes/page-001.jpg`, the student's notes, transcribed as `page-001.md`: Watt
  perfected the steam engine **in 1769**;
- `sources/book/page-001.jpg`, a textbook page (book «Historia 2», printed page 83,
  `book_page` in its sidecar), transcribed: the same, but **in 1765** -- the contradiction;
- `sources/pdf/page-001.pdf`, an imported PDF (pages 84-85 of «apuntes-profesor.pdf»);
- `sources/web/001-maquina-de-vapor.md`, a saved web page;
- one ended study session with two final segments.

`valid_notes()` passes the validator against this topic citing all four kinds and the
transcript; `side(...)`/`contradiction(...)` build `detect_contradictions` answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf

from studentassistant.observer import SEGMENT_EVENT_KIND
from studentassistant.sources import PageRange, import_pdf
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    set_book,
    sources_directory,
    start_session,
)

NOTES_ID = "sources/notes/page-001.jpg"
BOOK_ID = "sources/book/page-001.jpg"
PDF_ID = "sources/pdf/page-001.pdf"
PDF_PAGE_ID = "sources/pdf/page-001.pdf#page=1"
WEB_ID = "sources/web/001-maquina-de-vapor.md"

NOTES_TRANSCRIPTION = "# La máquina de vapor\n\nWatt perfeccionó la máquina de vapor en 1769.\n"
BOOK_TRANSCRIPTION = "James Watt perfeccionó la máquina de vapor en 1765.\n"
WEB_TEXT = "# Máquina de vapor\n\nLa máquina de Watt se usó en las fábricas textiles.\n"
BOOK_TITLE = "Historia 2"

NOTES_DEFINITION = "[Apuntes, página 1](../sources/notes/page-001.jpg)"
BOOK_DEFINITION = "[Libro «Historia 2», página 83](../sources/book/page-001.jpg)"
PDF_DEFINITION = "[PDF, página 84](../sources/pdf/page-001.pdf#page=1)"
WEB_DEFINITION = "[Web: Máquina de vapor](../sources/web/001-maquina-de-vapor.md)"


@dataclass(frozen=True)
class MultiSourceTopic:
    vault: Vault
    subject: str
    topic: str
    session: str

    def transcript_id(self, span: str = "00:00:02-00:00:08") -> str:
        return f"sessions/{self.session}#t={span}"


def _pdf() -> bytes:
    document = pymupdf.open()
    try:
        for number in range(1, 86):
            document.new_page().insert_text(
                (72, 72), f"Página {number}: la máquina de vapor", fontsize=14
            )
        return document.tobytes()
    finally:
        document.close()


def make_multi_source_topic(vault: Vault) -> MultiSourceTopic:
    subject = create_subject(vault, "Historia").slug
    topic = create_topic(vault, subject, "La máquina de vapor").slug
    session = start_session(vault, subject, topic, host="pc", protocol_version="1.1")

    put_source(vault, subject, topic, "notes", "page.jpg", b"\xff\xd8 notes", {})
    (sources_directory(vault, subject, topic, "notes") / "page-001.md").write_text(
        NOTES_TRANSCRIPTION, encoding="utf-8"
    )
    set_book(vault, subject, topic, BOOK_TITLE)
    put_source(vault, subject, topic, "book", "page.jpg", b"\xff\xd8 book", {"book_page": 83})
    (sources_directory(vault, subject, topic, "book") / "page-001.md").write_text(
        BOOK_TRANSCRIPTION, encoding="utf-8"
    )
    import_pdf(vault, subject, topic, "apuntes-profesor.pdf", _pdf(), pages=PageRange(84, 85))
    put_source(
        vault,
        subject,
        topic,
        "web",
        "Máquina de vapor",
        WEB_TEXT,
        {"title": "Máquina de vapor", "url": "https://example.org/maquina-de-vapor"},
    )

    session.append_event("session.started", "user", {})
    for segment_id, start, end, text in [
        ("s-1", 2_000, 7_500, "Watt la perfeccionó en mil setecientos sesenta y nueve."),
        ("s-2", 8_000, 12_000, "Luego se usó en las fábricas."),
    ]:
        session.append_transcript(start, end, text)
        session.append_event(
            SEGMENT_EVENT_KIND,
            "stt",
            {
                "segment_id": segment_id,
                "session_start_ms": start,
                "session_end_ms": end,
                "text": text,
                "language": "es",
            },
        )
    session.append_event("session.ended", "user", {})
    end_session(session)
    return MultiSourceTopic(vault=vault, subject=subject, topic=topic, session=session.id)


def valid_notes(session: str) -> str:
    return (
        "# La máquina de vapor\n"
        "\n"
        "## 1. Watt {#watt}\n"
        "\n"
        "Watt perfeccionó la máquina de vapor en 1769 (el libro dice 1765).[^p1][^t1][^b1]\n"
        "\n"
        "Se usó en las fábricas textiles.[^w1][^d1]\n"
        "\n"
        f"[^p1]: {NOTES_DEFINITION}\n"
        f"[^b1]: {BOOK_DEFINITION}\n"
        f"[^d1]: {PDF_DEFINITION}\n"
        f"[^w1]: {WEB_DEFINITION}\n"
        f"[^t1]: [Transcripción, 00:00:02–00:00:08](../sessions/{session}/transcript.jsonl"
        "#t=00:00:02-00:00:08)\n"
    )


def side(source_id: str, says: str) -> dict[str, Any]:
    return {"source_id": source_id, "says": says}


def contradiction(*sides: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    return {
        "text": text or "Los apuntes y el libro dan años distintos para la máquina de Watt.",
        "question": "¿En qué año perfeccionó Watt la máquina de vapor?",
        "sides": list(sides),
    }


def year_contradiction() -> dict[str, Any]:
    """The notes page (1769) against the book page (1765)."""
    return contradiction(side(NOTES_ID, "1769"), side(BOOK_ID, "1765"))
