"""Section-level edit ops (`editor.edits`): applied by anchor and block number, byte-stable."""

from __future__ import annotations

import pytest

from studentassistant.editor.edits import EditError, EditOp, NewFootnote, apply_edits
from studentassistant.editor.notes_format import parse, validate

NOTES = (
    "# Revolución\n"
    "\n"
    "## 1. Contexto {#contexto}\n"
    "\n"
    "Empezó en Gran Bretaña.[^p1]\n"
    "\n"
    "Las fábricas cambiaron la vida urbana.[^p1]\n"
    "\n"
    "## 2. Fechas {#fechas}\n"
    "\n"
    "La toma de la Bastilla fue en 1791.[^p1]\n"
    "\n"
    "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
)


def test_no_ops_leave_the_text_as_it_was() -> None:
    assert apply_edits(NOTES, []) == NOTES


def test_replace_block_changes_only_that_block() -> None:
    op = EditOp(op="replace_block", section="fechas", block=1, text="Fue en 1789.[^p1]")
    edited = apply_edits(NOTES, [op])
    assert edited == NOTES.replace("La toma de la Bastilla fue en 1791.", "Fue en 1789.")


def test_insert_after_and_at_the_start_with_new_footnotes() -> None:
    ops = [
        EditOp(op="insert_after", section="contexto", block=0, text="Intro.[^b1]"),
        EditOp(
            op="insert_after", section="contexto", block=1, text="Primero.[^p1]\n\nSegundo.[^p1]"
        ),
    ]
    footnotes = [
        NewFootnote(label="b1", definition="[Libro, página 3](../sources/book/page-003.jpg)")
    ]
    edited = apply_edits(NOTES, ops, footnotes)
    section = parse(edited).section("contexto")
    assert [b.text for b in section.blocks] == [
        "Intro.[^b1]",
        "Empezó en Gran Bretaña.[^p1]",
        "Primero.[^p1]",
        "Segundo.[^p1]",
        "Las fábricas cambiaron la vida urbana.[^p1]",
    ]
    assert edited.endswith(
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^b1]: [Libro, página 3](../sources/book/page-003.jpg)\n"
    )
    assert validate(edited) == []


def test_block_numbers_refer_to_the_notes_before_the_edit() -> None:
    ops = [
        EditOp(op="delete_block", section="contexto", block=1),
        EditOp(op="replace_block", section="contexto", block=2, text="Cambió la ciudad.[^p1]"),
    ]
    section = parse(apply_edits(NOTES, ops)).section("contexto")
    assert [b.text for b in section.blocks] == ["Cambió la ciudad.[^p1]"]


def test_replace_section_keeps_the_heading() -> None:
    op = EditOp(op="replace_section", section="contexto", text="Todo nuevo.[^p1]")
    edited = apply_edits(NOTES, [op])
    assert "## 1. Contexto {#contexto}\n\nTodo nuevo.[^p1]\n\n## 2. Fechas" in edited


def test_errors_are_collected_and_nothing_is_applied() -> None:
    ops = [
        EditOp(op="replace_block", section="nada", block=1, text="x[^p1]"),
        EditOp(op="replace_block", section="fechas", block=5, text="x[^p1]"),
        EditOp(op="insert_after", section="fechas", block=1, text="## Otra {#otra}"),
    ]
    footnotes = [
        NewFootnote(label="p1", definition="[Apuntes, página 2](../sources/notes/page-002.jpg)")
    ]
    with pytest.raises(EditError) as raised:
        apply_edits(NOTES, ops, footnotes)
    errors = raised.value.errors
    assert any("#nada" in e for e in errors)
    assert any("bloque 5" in e for e in errors)
    assert any("título de sección" in e for e in errors)
    assert any("[^p1] ya existe" in e for e in errors)


def test_the_same_footnote_again_is_not_duplicated() -> None:
    footnotes = [
        NewFootnote(label="p1", definition="[Apuntes, página 1](../sources/notes/page-001.jpg)")
    ]
    assert apply_edits(NOTES, [], footnotes) == NOTES


def test_two_ops_on_one_block_are_refused() -> None:
    ops = [
        EditOp(op="delete_block", section="fechas", block=1),
        EditOp(op="replace_block", section="fechas", block=1, text="x[^p1]"),
    ]
    with pytest.raises(EditError, match="más de una operación"):
        apply_edits(NOTES, ops)
