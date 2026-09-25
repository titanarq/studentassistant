"""`move_section` and the pruning of orphaned footnote definitions in `editor.edits` (#63)."""

from __future__ import annotations

import pytest

from studentassistant.editor.edits import EditError, EditOp, apply_edits
from studentassistant.editor.notes_format import parse, validate

NOTES = """# Tema

## 1. Uno {#uno}

Texto uno.[^a]

### 1.1. Uno punto uno {#uno-uno}

Texto uno uno.[^a]

## 2. Dos {#dos}

Texto dos.[^b]

## 3. Tres {#tres}

Texto tres.[^b]

[^a]: [Apuntes, página 1](../sources/notes/page-001.jpg)
[^b]: [Apuntes, página 2](../sources/notes/page-002.jpg)
"""


def _anchors(text: str) -> list[str | None]:
    return [section.anchor for section in parse(text).sections]


def _move(section: str, after: str | None) -> EditOp:
    return EditOp(op="move_section", section=section, after=after)


def test_a_section_moves_with_its_subsections_and_the_footnotes_stay_last() -> None:
    edited = apply_edits(NOTES, [_move("uno", "tres")])

    assert _anchors(edited) == ["dos", "tres", "uno", "uno-uno"]
    assert edited.endswith(
        "Texto uno uno.[^a]\n\n[^a]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^b]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
    )
    assert "Texto tres.[^b]\n\n## 1. Uno {#uno}" in edited
    assert validate(edited) == []


def test_the_last_section_moves_first_and_an_empty_after_means_first() -> None:
    edited = apply_edits(NOTES, [_move("tres", "")])

    assert _anchors(edited) == ["tres", "uno", "uno-uno", "dos"]
    assert edited.startswith("# Tema\n\n## 3. Tres {#tres}\n\nTexto tres.[^b]\n\n## 1. Uno")
    assert edited.endswith(
        "Texto dos.[^b]\n\n[^a]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^b]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
    )
    assert validate(edited) == []
    # Moving it back gives the original document, byte for byte.
    assert apply_edits(edited, [_move("tres", "dos")]) == NOTES


def test_moves_apply_in_order_after_the_block_ops() -> None:
    edited = apply_edits(
        NOTES,
        [
            EditOp(op="replace_block", section="dos", block=1, text="Dos, nuevo.[^b]"),
            _move("dos", None),
            _move("tres", "dos"),
        ],
    )

    assert _anchors(edited) == ["dos", "tres", "uno", "uno-uno"]
    assert "Dos, nuevo.[^b]" in edited
    assert validate(edited) == []


@pytest.mark.parametrize(
    ("ops", "message"),
    [
        ([_move("uno", "uno-uno")], "ella misma o una de sus subsecciones"),
        ([_move("uno", "nada")], "#nada"),
        ([_move("dos", "tres"), _move("dos", "uno")], "más de una operación move_section"),
    ],
)
def test_bad_moves_are_refused(ops: list[EditOp], message: str) -> None:
    with pytest.raises(EditError) as info:
        apply_edits(NOTES, ops)
    assert any(message in error for error in info.value.errors)


def test_a_definition_an_edit_orphans_is_removed() -> None:
    notes = NOTES.replace("Texto dos.[^b]", "Texto dos.[^b]\n\nInventado.[^ia]").replace(
        "[^b]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n",
        "[^b]: [Apuntes, página 2](../sources/notes/page-002.jpg)\n"
        "[^ia]: Ampliado por la IA: no está en tus fuentes\n",
    )
    assert validate(notes, "ampliado") == []

    edited = apply_edits(notes, [EditOp(op="delete_block", section="dos", block=2)])

    assert edited == NOTES
    assert validate(edited, "estricto") == []


def test_a_definition_still_cited_elsewhere_is_kept() -> None:
    edited = apply_edits(NOTES, [EditOp(op="delete_block", section="tres", block=1)])

    assert "[^b]: [Apuntes, página 2]" in edited
    assert validate(edited) == []
