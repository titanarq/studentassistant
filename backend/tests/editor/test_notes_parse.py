"""Parsing `apuntes.md`: sections with anchors, blocks with their footnotes, definitions."""

from __future__ import annotations

from notes_samples import read_fixture

from studentassistant.editor.notes_format import parse


def test_the_fixture_has_its_preamble_and_every_section_with_its_anchor() -> None:
    notes = parse(read_fixture("apuntes.md"))

    assert [b.kind for b in notes.preamble] == ["title", "paragraph"]
    assert [(s.heading.level, s.heading.title, s.anchor) for s in notes.sections] == [
        (2, "1. Contexto", "contexto"),
        (2, "2. Causas", "causas"),
        (3, "2.1. La máquina de vapor", "maquina-de-vapor"),
    ]


def test_blocks_carry_their_kind_and_footnote_references() -> None:
    notes = parse(read_fixture("apuntes.md"))

    assert [(b.kind, b.footnote_refs) for b in notes.section("contexto").blocks] == [
        ("paragraph", ["p1", "t1"]),
    ]
    assert [(b.kind, b.footnote_refs) for b in notes.section("causas").blocks] == [
        ("list", ["p2", "t2"]),
        ("table", ["b12"]),
    ]
    last = notes.section("maquina-de-vapor").blocks
    assert [(b.kind, b.footnote_refs) for b in last] == [
        ("paragraph", ["pdf3", "w1"]),
        ("rule", []),
        ("footnotes", []),
    ]


def test_a_loose_list_with_a_nested_paragraph_is_one_list_item_group() -> None:
    (group,) = [b for b in parse(read_fixture("apuntes.md")).section("causas").blocks[:1]]

    assert group.text.startswith("- Crecimiento")
    assert group.text.endswith("al final de la clase.[^t2]")
    assert "\n\n- Capital" in group.text


def test_footnote_definitions_are_collected_in_order() -> None:
    notes = parse(read_fixture("apuntes.md"))

    assert [d.label for d in notes.footnotes] == ["p1", "p2", "t1", "t2", "b12", "pdf3", "w1"]
    assert notes.footnotes[0].text == "[Apuntes, página 1](../sources/notes/page-001.jpg)"


def test_a_definition_keeps_its_indented_continuation_lines() -> None:
    notes = parse("## A {#a}\n\nTexto.[^x]\n\n[^x]: primera línea\n    segunda línea\n")

    (definition,) = notes.footnotes
    assert definition.text == "primera línea\n    segunda línea"


def test_a_heading_without_anchor_is_a_section_with_no_anchor() -> None:
    (section,) = parse("## Sin ancla\n\nTexto.[^p1]\n").sections

    assert section.anchor is None
    assert section.heading.title == "Sin ancla"


def test_a_definition_line_right_after_a_paragraph_starts_a_footnotes_block() -> None:
    (section,) = parse("## A {#a}\nTexto.[^p1]\n[^p1]: [Apuntes, página 1](x)\n").sections

    assert [b.kind for b in section.blocks] == ["paragraph", "footnotes"]
    assert section.blocks[0].trailing == "\n"


def test_references_are_listed_once_each_and_definitions_are_not_references() -> None:
    (section,) = parse("## A {#a}\n\nUno.[^p1] Dos.[^p1][^p2]\n\n[^p1]: x\n").sections

    assert section.blocks[0].footnote_refs == ["p1", "p2"]
    assert section.blocks[1].footnote_refs == []
