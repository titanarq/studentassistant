"""Images of the notes -- an SVG diagram the editor drew (#511) -- in the PDF, Anki and Marp
exports."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pymupdf
import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.diagram import DrawnDiagram, store_diagram
from studentassistant.generators import GeneratorRegistry, run_generator
from studentassistant.generators import exam as exam_module
from studentassistant.generators import flashcards as flashcards_module
from studentassistant.generators import slides as slides_module
from studentassistant.generators.exam import inline_html, text_html
from studentassistant.generators.flashcards import anki_html
from studentassistant.generators.images import note_images, split_images, svg_to_png
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory, read_notes, write_notes

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 100">'
    '<polygon points="10,90 110,90 60,10" fill="none" stroke="black" stroke-width="2"/>'
    '<text x="62" y="50" font-size="10">h</text></svg>'
)
LINK = "![Diagrama 1](../sources/images/img-001.svg)"


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _with_diagram(topic: ReviseTopic) -> DrawnDiagram:
    """A diagram stored and shown, cited, at the end of the notes' #definicion section."""
    diagram = _run(store_diagram(topic.vault, topic.subject, topic.topic, SVG, "Triángulo"))
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    notes = notes.replace(
        "Se escribe $f'(x)$.[^t2]\n",
        f"Se escribe $f'(x)$.[^t2]\n\n{diagram.markdown}[^img001]\n",
        1,
    )
    write_notes(topic.vault, topic.subject, topic.topic, f"{notes.rstrip()}\n{diagram.footnote}\n")
    GitSync(topic.vault).checkpoint("fixture: diagram")
    return diagram


def _generate(topic: ReviseTopic, generator: type, tool: str, output: dict[str, Any]) -> None:
    fake = FakeClaude().reply_tool(tool, output)
    registry = GeneratorRegistry()
    registry.register(generator)
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            generator.kind,
            client=client,
            sync=GitSync(topic.vault),
            registry=registry,
            options={},
            clock=lambda: NOW,
        )
    )


def _file(topic: ReviseTopic, name: str) -> bytes:
    return (generated_directory(topic.vault, topic.subject, topic.topic) / name).read_bytes()


def test_split_images_and_the_fallback_text() -> None:
    assert list(split_images(f"Mira: {LINK} ya")) == [
        "Mira: ",
        ("Diagrama 1", "sources/images/img-001.svg"),
        " ya",
    ]
    assert inline_html(f"a {LINK}") == "a [Imagen: Diagrama 1]"
    assert anki_html(LINK) == "[Imagen: Diagrama 1]"
    assert text_html(LINK, {"sources/images/img-001.svg": "img-001.png"}) == (
        '<p><img src="img-001.png" alt="Diagrama 1"/></p>'
    )
    # Mermaid is untouched by image handling.
    assert "<pre>flowchart TD</pre>" in text_html("```mermaid\nflowchart TD\n```")


def test_note_images_reads_only_the_topic_images_the_texts_link(topic: ReviseTopic) -> None:
    diagram = _with_diagram(topic)
    texts = [LINK, "![x](../sources/images/img-009.png)", "![y](https://evil.example/a.svg)"]

    found = note_images(topic.vault, topic.subject, topic.topic, texts)

    assert list(found) == [diagram.source_id]
    assert found[diagram.source_id].media_type == "image/svg+xml"
    png = svg_to_png(found[diagram.source_id].data)
    assert png.startswith(b"\x89PNG")


def test_the_exam_pdf_draws_a_diagram_a_question_links(topic: ReviseTopic) -> None:
    _with_diagram(topic)
    question = {
        "statement": f"Observa el triángulo:\n\n{LINK}\n\n¿Qué segmento es la altura?",
        "difficulty": "media",
        "solution": f"La altura es h.\n\n{LINK}",
        "rubric": [{"criterion": "Nombra h", "points": 10}],
        "anchors": ["definicion"],
        "points": 10,
    }
    output = {"instructions": "Responde.", "exercises": [], "exam": [question]}

    _generate(topic, exam_module.ExamGenerator, exam_module.TOOL_NAME, output)

    for name in (exam_module.EXAM_PDF, exam_module.SOLUTIONS_PDF):
        with pymupdf.open("pdf", _file(topic, name)) as document:
            assert any(page.get_images() for page in document), name
            assert "[Imagen:" not in "".join(page.get_text() for page in document)
    # The Markdown keeps the link, which resolves from generated/ as from notes/.
    assert LINK in _file(topic, exam_module.EXAM_MD).decode()


def test_the_anki_package_ships_a_linked_diagram_as_media(
    topic: ReviseTopic, tmp_path: Any
) -> None:
    diagram = _with_diagram(topic)
    cards = [
        {"front": f"¿Qué segmento es la altura?\n{LINK}", "back": "h", "anchors": ["definicion"]}
    ]

    _generate(
        topic,
        flashcards_module.FlashcardsGenerator,
        flashcards_module.TOOL_NAME,
        {"cards": cards},
    )

    media_name = f"sa-{topic.subject}-{topic.topic}-img-001.svg"
    with zipfile.ZipFile(io.BytesIO(_file(topic, flashcards_module.APKG_NAME))) as archive:
        assert json.loads(archive.read("media")) == {"0": media_name}
        stored = archive.read("0")
        database = archive.read("collection.anki2")
    assert stored.startswith(b"<?xml") and b"<polygon" in stored
    assert diagram.meta["sha256"]
    path = tmp_path / "collection.anki2"
    path.write_bytes(database)
    with sqlite3.connect(path) as db:
        [(fields,)] = db.execute("SELECT flds FROM notes").fetchall()
    assert f'<img src="{media_name}" alt="Diagrama 1">' in fields.split("\x1f")[0]


class _Exporter:
    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}

    async def export(self, markdown: str, assets: dict[str, bytes]) -> slides_module.Exported:
        self.assets = assets
        return slides_module.Exported(files={slides_module.PDF_NAME: b"%PDF-1.7 fake"})


def test_a_diagram_is_a_figure_a_slide_can_show(
    topic: ReviseTopic, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_diagram(topic)
    exporter = _Exporter()
    monkeypatch.setattr(slides_module.SlidesGenerator, "exporter", exporter)
    deck = {
        "title": "Derivadas",
        "subtitle": "",
        "slides": [
            {
                "title": "El triángulo",
                "bullets": ["**Derivada**: el límite del cociente incremental."],
                "anchors": ["definicion"],
                "image": "f2",
            }
        ],
    }

    _generate(topic, slides_module.SlidesGenerator, slides_module.TOOL_NAME, deck)

    markdown = _file(topic, slides_module.MARKDOWN_NAME).decode()
    assert "![bg right:40% contain](diapositivas/figura-01.svg)" in markdown
    assert '<!-- _footer: "Imagen: Diagrama 1" -->' in markdown
    assert b"<polygon" in _file(topic, "diapositivas/figura-01.svg")
    assert list(exporter.assets) == ["diapositivas/figura-01.svg"]
