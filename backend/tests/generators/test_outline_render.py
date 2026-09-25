"""Rendering the outline: nested Markdown plus a mermaid `mindmap`, checked against the golden."""

from __future__ import annotations

import re

import pytest

from outline_drafts import DRAFT, GOLDEN, TOPIC_TITLE
from studentassistant.generators.outline import (
    Outline,
    OutlineDraft,
    OutlineNode,
    mermaid_label,
    render_mindmap,
    render_outline,
)

KNOWN = {"definicion", "proximo-dia"}


def _outline() -> Outline:
    return OutlineDraft.model_validate(DRAFT).to_outline(TOPIC_TITLE)


def _mermaid(text: str) -> str:
    [block] = re.findall(r"```mermaid\n(.*?)```\n", text, flags=re.DOTALL)
    return block


def test_renders_the_golden_fixture() -> None:
    assert render_outline(_outline(), known_anchors=KNOWN) == GOLDEN.read_text(encoding="utf-8")


def test_the_markdown_nests_the_outline_and_links_every_node_to_its_sections() -> None:
    text = render_outline(_outline(), known_anchors=KNOWN)
    assert text.startswith("# Esquema: Derivadas\n")
    assert "\n## 1. Definición de derivada\n" in text and "\n## 2. Próximo día\n" in text
    assert "\n  - **1.1.1 Límite cuando h -> 0**: " in text
    body = text.split("## Mapa mental")[0]
    node_lines = [line for line in body.splitlines() if line.startswith(("- ", "  - "))]
    node_lines += [line for line in body.splitlines() if line.startswith(("Apuntes:", "El "))]
    assert len(node_lines) == 6
    assert all("](../notes/apuntes.md#" in line for line in node_lines)
    # `<` would be read as HTML on GitHub.
    assert "Regla de la cadena &lt;mañana>" in text


def test_the_mind_map_is_one_mindmap_block_with_every_title() -> None:
    block = _mermaid(render_outline(_outline()))
    lines = block.splitlines()
    assert lines[0] == "mindmap"
    assert lines[1] == '  root(("Derivadas"))'
    assert lines[2] == '    n1("Definición de derivada")'
    assert lines[4] == '        n1_1_1["Límite cuando h -> 0"]'
    assert len(lines) == 2 + 6
    # Every label is one quoted string with no quote inside.
    for line in lines[1:]:
        label = re.search(r'"(.*)"', line)
        assert label is not None and '"' not in label.group(1)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Causas (y efectos) [1] {a}", '"Causas (y efectos) [1] {a}"'),
        ('Notación "prima"', "\"Notación 'prima'\""),
        ("Código `x`", "\"Código 'x'\""),
        ("a <b> c < d", '"a ‹b› c ‹ d"'),
        ("h -> 0", '"h -> 0"'),
        ("C#quot; y C# ;", '"C# quot; y C# ;"'),
        ("100%% seguro", '"100% % seguro"'),
        ("dos\nlíneas", '"dos líneas"'),
    ],
)
def test_mermaid_labels_are_quoted_and_escaped(text: str, label: str) -> None:
    assert mermaid_label(text) == label


def test_a_node_without_a_resolvable_anchor_is_marked_not_linked() -> None:
    outline = Outline(
        title="Tema",
        nodes=[
            OutlineNode(title="Sin ancla"),
            OutlineNode(title="Ancla rara", anchors=["inventada", "definicion"]),
        ],
    )
    text = render_outline(outline, known_anchors=KNOWN)
    assert "Apuntes: *(sin sección de los apuntes)*" in text
    assert "`#inventada` *(no está en los apuntes)*" in text
    assert "[#definicion](../notes/apuntes.md#definicion)" in text
    # Without the notes' anchors every anchor is linked.
    assert "[#inventada](../notes/apuntes.md#inventada)" in render_outline(outline)


def test_the_mind_map_alone() -> None:
    block = render_mindmap(Outline(title="Tema", nodes=[OutlineNode(title="Idea")]))
    assert block == '```mermaid\nmindmap\n  root(("Tema"))\n    n1("Idea")\n```\n'
