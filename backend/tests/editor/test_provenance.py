"""Provenance footnotes: every ADR-0005 source kind, built and parsed as links GitHub follows."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from studentassistant.editor.notes_format import (
    IA_PROVENANCE,
    FootnoteDefinition,
    Provenance,
    ProvenanceError,
    page_provenance,
    parse,
    parse_provenance,
    pdf_provenance,
    transcript_provenance,
    web_provenance,
)

CASES = [
    (
        page_provenance("notes", 4),
        "notes",
        "sources/notes/page-004.jpg",
        "sources/notes/page-004.jpg",
        "[^p4]: [Apuntes, página 4](../sources/notes/page-004.jpg)",
    ),
    (
        page_provenance("book", 12, ".PNG"),
        "book",
        "sources/book/page-012.png",
        "sources/book/page-012.png",
        "[^p4]: [Libro, página 12](../sources/book/page-012.png)",
    ),
    (
        pdf_provenance(1, page=3),
        "pdf",
        "sources/pdf/page-001.pdf#page=3",
        "sources/pdf/page-001.pdf",
        "[^p4]: [PDF, página 3](../sources/pdf/page-001.pdf#page=3)",
    ),
    (
        pdf_provenance(2, "png"),
        "pdf",
        "sources/pdf/page-002.png",
        "sources/pdf/page-002.png",
        "[^p4]: [PDF, página 2](../sources/pdf/page-002.png)",
    ),
    (
        web_provenance("001-maquina-de-vapor.md", "Máquina de vapor"),
        "web",
        "sources/web/001-maquina-de-vapor.md",
        "sources/web/001-maquina-de-vapor.md",
        "[^p4]: [Web: Máquina de vapor](../sources/web/001-maquina-de-vapor.md)",
    ),
    (
        transcript_provenance("20260924-183000", 154, 190),
        "transcript",
        "sessions/20260924-183000#t=00:02:34-00:03:10",
        "sessions/20260924-183000/transcript.jsonl",
        "[^p4]: [Transcripción, 00:02:34–00:03:10]"
        "(../sessions/20260924-183000/transcript.jsonl#t=00:02:34-00:03:10)",
    ),
]


@pytest.mark.parametrize(("provenance", "kind", "source_id", "path", "line"), CASES)
def test_each_kind_builds_a_definition_that_parses_back(
    provenance: Provenance, kind: str, source_id: str, path: str, line: str
) -> None:
    assert (provenance.kind, provenance.source_id, provenance.path) == (kind, source_id, path)
    assert provenance.definition("p4") == line

    (definition,) = parse(f"## A {{#a}}\n\nTexto.[^p4]\n\n{line}\n").footnotes
    assert parse_provenance(definition) == provenance


@pytest.mark.parametrize(("provenance", "kind", "source_id", "path", "line"), CASES)
def test_the_link_resolves_from_notes_apuntes_md_to_the_source_file(
    provenance: Provenance, kind: str, source_id: str, path: str, line: str
) -> None:
    link = provenance.link
    assert link is not None and link.startswith("../")
    resolved = PurePosixPath("topic/notes") / link.partition("#")[0]
    # What GitHub does with a relative link from `notes/apuntes.md`: land on the cited file.
    assert str(resolved).replace("topic/notes/../", "topic/") == f"topic/{path}"


def test_ia_is_the_special_footnote_with_its_spanish_text() -> None:
    assert IA_PROVENANCE.definition("cualquiera") == (
        "[^ia]: Ampliado por la IA: no está en tus fuentes"
    )
    assert IA_PROVENANCE.link is None
    ia = parse_provenance(FootnoteDefinition(label="ia", text="lo que sea"))
    assert ia.kind == "ia"
    assert ia.path is None


def test_transcript_times_run_past_an_hour() -> None:
    assert transcript_provenance("20260924-183000", 3725, 3790).source_id == (
        "sessions/20260924-183000#t=01:02:05-01:03:10"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Apuntes, página 4",
        "[Apuntes](sources/notes/page-004.jpg)",
        "[Apuntes](/sources/notes/page-004.jpg)",
        "[Web](https://es.wikipedia.org/wiki/Revolución_Industrial)",
        "[Apuntes](../sources/notes/../../vault.yaml)",
        "[Apuntes](../sources/video/page-004.mp4)",
        "[Transcripción](../sessions/20260924-183000/transcript.jsonl)",
        "[Uno](../sources/notes/page-001.jpg) y [otro](../sources/notes/page-002.jpg)",
    ],
)
def test_anything_but_one_relative_link_to_a_known_source_is_refused(text: str) -> None:
    with pytest.raises(ProvenanceError, match=r"\[\^x\]"):
        parse_provenance(FootnoteDefinition(label="x", text=text))
