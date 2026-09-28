"""Mermaid fences in the printed exports: the code block plus a Spanish note (#481)."""

from __future__ import annotations

from studentassistant.generators.diagrams import DIAGRAM_UNAVAILABLE, fences, note_mermaid_fences
from studentassistant.generators.slides import DraftDeck, DraftSlide, render_markdown

NOTE = f"*{DIAGRAM_UNAVAILABLE}*"


def test_fences_are_found_with_their_language_and_body() -> None:
    lines = ["a", "  ```Mermaid title", "  flowchart TD", "    A --> B", "  ```", "~~~", "x"]
    found = list(fences(lines))
    assert [(f.lang, f.body_lines, f.indent, f.start, f.end, f.closed) for f in found] == [
        ("Mermaid", ("flowchart TD", "  A --> B"), "  ", 1, 5, True),
        ("", ("x",), "", 5, 7, False),
    ]
    assert found[0].is_mermaid and not found[1].is_mermaid


def test_the_note_follows_every_closed_mermaid_fence_only() -> None:
    markdown = (
        "Texto\n```mermaid\nflowchart TD\n  A --> B\n```\nSigue.\n\n"
        "```python\nprint(1)\n```\n\n- punto\n  ```mermaid\n  mindmap\n    root((A))\n  ```"
    )
    assert note_mermaid_fences(markdown) == (
        f"Texto\n```mermaid\nflowchart TD\n  A --> B\n```\n{NOTE}\n\nSigue.\n\n"
        f"```python\nprint(1)\n```\n\n- punto\n  ```mermaid\n  mindmap\n    root((A))\n  ```\n"
        f"  {NOTE}"
    )
    assert note_mermaid_fences("```mermaid\nsin cerrar") == "```mermaid\nsin cerrar"
    assert note_mermaid_fences("Sin diagramas.") == "Sin diagramas."


def test_a_mermaid_bullet_keeps_the_indentation_of_its_diagram() -> None:
    bullet = "Mapa:\n```mermaid\nmindmap\n  root((A))\n    B\n```\nFin."
    deck = DraftDeck(title="T", slides=[DraftSlide(title="S", bullets=[bullet])])
    markdown = render_markdown(deck, {}, subject_name="X")
    assert "- Mapa:\n  ```mermaid\n  mindmap\n    root((A))\n      B\n  ```\n  Fin.\n" in markdown
