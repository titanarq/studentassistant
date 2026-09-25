"""The outline model: the `Outline` tree, the flat `OutlineDraft` Claude answers and its checks."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from outline_drafts import DRAFT, TOPIC_TITLE
from studentassistant.generators.outline import (
    MAX_DEPTH,
    TOOL_NAME,
    Outline,
    OutlineDraft,
    OutlineNode,
)
from studentassistant.llm.structured import strict_tool


def _node(node_id: str, parent: str | None, title: str = "Idea", **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "parent": parent,
        "title": title,
        "gloss": None,
        "anchors": ["a"],
    } | extra


def _chain(length: int) -> list[dict[str, Any]]:
    return [_node(f"n{i}", f"n{i - 1}" if i else None) for i in range(length)]


def test_a_draft_becomes_the_tree_in_document_order() -> None:
    outline = OutlineDraft.model_validate(DRAFT).to_outline(TOPIC_TITLE)

    assert outline.title == "Derivadas"
    assert [node.title for node in outline.nodes] == ["Definición de derivada", "Próximo día"]
    walked = [(number, level, node.title) for number, level, node in outline.walk()]
    assert [(number, level) for number, level, _ in walked] == [
        ("1", 1),
        ("1.1", 2),
        ("1.1.1", 3),
        ("1.2", 2),
        ("2", 1),
        ("2.1", 2),
    ]
    first = outline.nodes[0]
    assert first.gloss == "El límite del cociente incremental" and first.depth() == 3
    # A leading `#` is dropped from an anchor.
    assert first.children[0].children[0].anchors == ["definicion"]


def test_provenance_is_one_item_per_node_named_by_its_number() -> None:
    outline = OutlineDraft.model_validate(DRAFT).to_outline(TOPIC_TITLE)
    items = outline.provenance()
    assert [item.item for item in items] == ["1", "1.1", "1.1.1", "1.2", "2", "2.1"]
    assert items[-1].anchors == ["proximo-dia", "definicion"]


def test_titles_and_glosses_are_one_line_and_anchors_are_cleaned() -> None:
    node = OutlineNode(
        title="  Regla\n de la   cadena ",
        gloss="  \n ",
        anchors=["#a", " a ", "", "b", "#"],
    )
    assert node.title == "Regla de la cadena"
    assert node.gloss is None
    assert node.anchors == ["a", "b"]


@pytest.mark.parametrize("title", ["", "   ", "\n"])
def test_an_empty_title_is_refused(title: str) -> None:
    with pytest.raises(ValidationError, match="empty"):
        OutlineNode(title=title)
    with pytest.raises(ValidationError, match="empty"):
        Outline(title=title, nodes=[OutlineNode(title="Idea")])
    with pytest.raises(ValidationError, match="empty title"):
        OutlineDraft.model_validate({"nodes": [_node("n1", None, title)]})


def test_an_outline_needs_nodes() -> None:
    with pytest.raises(ValidationError):
        Outline(title="Tema", nodes=[])
    with pytest.raises(ValidationError, match="no nodes"):
        OutlineDraft.model_validate({"nodes": []})


def test_depth_is_capped() -> None:
    deepest = OutlineDraft.model_validate({"nodes": _chain(MAX_DEPTH)}).to_outline("Tema")
    assert deepest.nodes[0].depth() == MAX_DEPTH

    with pytest.raises(ValidationError, match=f"at most {MAX_DEPTH}"):
        OutlineDraft.model_validate({"nodes": _chain(MAX_DEPTH + 1)})

    node = OutlineNode(title="hoja")
    for level in range(MAX_DEPTH):
        node = OutlineNode(title=f"nivel {level}", children=[node])
    with pytest.raises(ValidationError, match=f"at most {MAX_DEPTH}"):
        Outline(title="Tema", nodes=[node])


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([_node("n1", None), _node("n1", None)], "repeated"),
        ([_node("n1", None), _node("n2", "nx")], "not an earlier node"),
        ([_node("n2", "n1"), _node("n1", None)], "not an earlier node"),
        ([_node("n1", "n1")], "not an earlier node"),
    ],
)
def test_a_draft_that_is_not_a_tree_is_refused(nodes: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        OutlineDraft.model_validate({"nodes": nodes})


def test_the_tool_schema_is_strict_and_not_recursive() -> None:
    tool = strict_tool(TOOL_NAME, "Record the outline.", OutlineDraft)
    schema = tool["input_schema"]
    assert tool["strict"] is True
    # Strict tools refuse recursive schemas: no definition may refer to itself.
    for name, definition in schema.get("$defs", {}).items():
        assert f"#/$defs/{name}" not in json.dumps(definition)
    assert schema["additionalProperties"] is False
