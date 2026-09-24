"""Serializing a parse gives back the input byte for byte, whatever the input is."""

from __future__ import annotations

import pytest
from notes_samples import NOTES_FIXTURES

from studentassistant.editor.notes_format import Block, parse, serialize


@pytest.mark.parametrize("path", sorted(NOTES_FIXTURES.glob("*.md")), ids=lambda p: p.name)
def test_every_fixture_round_trips_byte_for_byte(path) -> None:
    text = path.read_bytes().decode("utf-8")

    assert serialize(parse(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "sin salto final",
        "## A {#a}",
        "## A {#a}\nTexto.[^p1]",
        "\n\n# Título\n\n\n## A {#a}\n\n\nTexto.[^p1]\n\n\n",
        "## A {#a}\r\n\r\nTexto con CRLF.[^p1]\r\n\r\n[^p1]: x\r\n",
        "## A {#a}   \n\nEspacios al final   \n  \t\n- uno\n\n  sigue\n- dos\n\n\n| a |\n|---|\n",
        "# Solo título\n",
        "## A {#a}\n\nUna\u2028línea\x0cextraña.[^p1]\n",
        "texto\n## A {#a}\ntexto pegado\n### B\n***\n1. uno\n2) dos\n[^x]: def\ncontinúa\n",
    ],
)
def test_odd_inputs_round_trip_byte_for_byte(text: str) -> None:
    assert serialize(parse(text)) == text


def test_a_changed_block_serializes_in_place() -> None:
    notes = parse("## A {#a}\n\nViejo.[^p1]\n\n[^p1]: x\n")
    section = notes.sections[0]
    new_block = Block(kind="paragraph", text="Nuevo.[^p1]", trailing="\n\n")
    changed = notes.model_copy(
        update={
            "sections": [
                section.model_copy(update={"blocks": [new_block, *section.blocks[1:]]}),
            ]
        }
    )

    assert serialize(changed) == "## A {#a}\n\nNuevo.[^p1]\n\n[^p1]: x\n"
