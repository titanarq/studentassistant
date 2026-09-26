"""`add_section` in `editor.edits`: a document grows by sections, the footnotes run stays last."""

from __future__ import annotations

import pytest

from studentassistant.editor.edits import EditError, EditOp, NewFootnote, apply_edits
from studentassistant.editor.notes_format import parse, serialize, validate

NOTES = """# Tema

## 1. Uno {#uno}

Texto uno.[^a]

### 1.1. Uno punto uno {#uno-uno}

Texto uno uno.[^a]

## 2. Dos {#dos}

Texto dos.[^a]

[^a]: [Apuntes, página 1](../sources/notes/page-001.jpg)
"""


def _add(anchor: str, after: str | None = None, level: int = 2, text: str | None = None) -> EditOp:
    return EditOp(
        op="add_section", after=after, level=level, title=anchor.title(), anchor=anchor, text=text
    )


def _anchors(text: str) -> list[str | None]:
    return [section.anchor for section in parse(text).sections]


def test_a_section_goes_after_another_and_its_subsections() -> None:
    edited = apply_edits(NOTES, [_add("nueva", "uno", text="Algo nuevo.[^a]")])

    assert _anchors(edited) == ["uno", "uno-uno", "nueva", "dos"]
    assert "### 1.1. Uno punto uno {#uno-uno}\n\nTexto uno uno.[^a]\n\n## Nueva {#nueva}\n\n" in (
        edited
    )
    assert "## Nueva {#nueva}\n\nAlgo nuevo.[^a]\n\n## 2. Dos {#dos}" in edited
    assert validate(edited) == []
    assert serialize(parse(edited)) == edited


def test_first_and_last_positions_keep_the_footnotes_run_at_the_end() -> None:
    edited = apply_edits(
        NOTES, [_add("primera"), _add("ultima", "dos", level=3, text="Final.[^a]")]
    )

    assert _anchors(edited) == ["primera", "uno", "uno-uno", "dos", "ultima"]
    assert edited.startswith("# Tema\n\n## Primera {#primera}\n\n## 1. Uno {#uno}\n")
    assert edited.endswith(
        "### Ultima {#ultima}\n\nFinal.[^a]\n\n"
        "[^a]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
    )
    assert validate(edited) == []


def test_a_bare_title_grows_into_a_document() -> None:
    edited = apply_edits(
        "# Tema\n",
        [_add("uno", text="Uno.[^est]"), _add("dos", "uno", text="Dos.[^est]")],
        [NewFootnote(label="est", definition="Escrito por el estudiante")],
    )

    assert edited == (
        "# Tema\n\n## Uno {#uno}\n\nUno.[^est]\n\n## Dos {#dos}\n\nDos.[^est]\n\n"
        "[^est]: Escrito por el estudiante\n"
    )


def test_a_new_section_can_be_moved_in_the_same_edit() -> None:
    edited = apply_edits(
        NOTES, [_add("nueva", "dos"), EditOp(op="move_section", section="nueva", after=None)]
    )
    assert _anchors(edited) == ["nueva", "uno", "uno-uno", "dos"]


@pytest.mark.parametrize(
    ("op", "message"),
    [
        (_add("uno"), "ya hay una sección con el ancla #uno"),
        (_add("mal ancla"), "necesita un ancla"),
        (_add("nueva", "no-existe"), "no hay ninguna sección con el ancla #no-existe"),
        (_add("nueva", level=1), "el nivel (`level`) debe ser de 2 a 6"),
        (EditOp(op="add_section", anchor="x", level=2, title=" "), "necesita un título"),
        (_add("nueva", text="## Dentro\n\nTexto.[^a]"), "contiene un título de sección"),
    ],
)
def test_a_bad_add_section_applies_nothing(op: EditOp, message: str) -> None:
    with pytest.raises(EditError) as caught:
        apply_edits(NOTES, [op, EditOp(op="delete_block", section="dos", block=1)])
    assert any(message in error for error in caught.value.errors), caught.value.errors


def test_two_new_sections_with_one_anchor_are_refused() -> None:
    with pytest.raises(EditError) as caught:
        apply_edits(NOTES, [_add("nueva"), _add("nueva", "dos")])
    assert any("ya hay una sección con el ancla #nueva" in e for e in caught.value.errors)
