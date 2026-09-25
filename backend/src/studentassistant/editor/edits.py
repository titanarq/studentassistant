"""Section-level edit ops over `notes/apuntes.md`: how the editor changes notes it already wrote.

The editor never rewrites the whole document to change one thing: it names what to change by
section anchor (`{#causas}`) and block number, and `apply_edits` applies that to the parsed notes
(`notes_format.parse`), leaving every other byte as it was. The result is then checked with the
provenance validator like any other version of the notes (ADR-0005); an op that does not apply
is reported, never guessed at.

Ops (`EditOp`, a flat model so it can be a strict tool input):

- `replace_block` (`section`, `block`, `text`): block `block` of the section becomes `text`;
- `insert_after` (`section`, `block`, `text`): `text` goes after block `block` (`0`: at the start
  of the section, before its first block);
- `delete_block` (`section`, `block`): block `block` is removed;
- `replace_section` (`section`, `text`): every block of the section is replaced by `text` (its
  heading, and so its anchor, stays).

Blocks are numbered from 1 within their section, as the validator's messages number them ("Sección
#causas, bloque 2"), and every number of one edit refers to the notes as they were before any op
of it: the ops do not renumber each other. `text` is one or more blocks separated by blank lines,
with no section heading in it. New footnote definitions (`NewFootnote`: `label`, `definition` --
what follows `[^label]: `) are appended to the run of definitions that ends the document (one is
started when there is none); a label already defined is fine when its definition is the same and
an error otherwise.

Pure code: no file system, no LLM. Used by the doubts resolution (`doubts.py`) and meant for the
conversational edit loop as well.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.notes_format import Block, NotesDocument, Section, parse, serialize

EditOpName = Literal["replace_block", "insert_after", "delete_block", "replace_section"]
EDIT_OP_NAMES: tuple[str, ...] = (
    "replace_block",
    "insert_after",
    "delete_block",
    "replace_section",
)

_LABEL = re.compile(r"^[A-Za-z0-9_-]+$")


class EditOp(BaseModel):
    """One change to the notes; see the module docstring for what each `op` takes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: EditOpName
    section: str = Field(description="The anchor of the section, without `#`.")
    block: int | None = Field(
        default=None,
        description="The block number within the section, from 1 (0 = before the first block,"
        " only for insert_after); not used by replace_section.",
    )
    text: str | None = Field(
        default=None,
        description="The new Markdown blocks (not for delete_block), each with its footnotes.",
    )


class NewFootnote(BaseModel):
    """A footnote definition the edited notes need: `[^label]: definition`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    definition: str = Field(
        description="What follows `[^label]: `, e.g. `[Apuntes, página 1](...)`."
    )

    def line(self) -> str:
        return f"[^{self.label}]: {self.definition.strip()}"


class EditError(ValueError):
    """Edit ops that cannot be applied; `errors` are Spanish, for re-asking the editor."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def _new_blocks(text: str, where: str, errors: list[str]) -> list[Block]:
    body = text.strip("\n")
    if not body.strip():
        errors.append(f"{where}: el texto está vacío; para quitar un bloque usa delete_block.")
        return []
    parsed = parse(body + "\n")
    if parsed.sections:
        errors.append(
            f"{where}: el texto contiene un título de sección («{parsed.sections[0].heading.raw}»);"
            " una operación solo cambia bloques dentro de una sección."
        )
        return []
    return list(parsed.preamble)


def _has_blank_after(block: Block) -> bool:
    return block.trailing.count("\n") >= 2


def _with_blank(block: Block) -> Block:
    if _has_blank_after(block):
        return block
    return block.model_copy(update={"trailing": block.trailing.rstrip("\n") + "\n\n"})


def _rebuild_section(
    section: Section, ops: list[EditOp], errors: list[str]
) -> tuple[list[Block], set[int]]:
    """The new blocks of `section` and the positions (in the result) of the new ones."""
    name = f"Sección #{section.anchor}"
    count = len(section.blocks)
    whole = [op for op in ops if op.op == "replace_section"]
    if whole:
        if len(ops) > 1:
            errors.append(
                f"{name}: replace_section no se puede combinar con otras operaciones en la misma"
                " sección."
            )
            return list(section.blocks), set()
        op = whole[0]
        if op.text is None or not op.text.strip():
            return [], set()
        blocks = _new_blocks(op.text, name, errors)
        if blocks and section.blocks:
            last = blocks[-1].model_copy(update={"trailing": section.blocks[-1].trailing})
            blocks[-1] = last
        return blocks, set(range(len(blocks)))

    replaced: dict[int, EditOp] = {}
    inserted: dict[int, list[EditOp]] = {}
    for op in ops:
        where = f"{name}, bloque {op.block}"
        if op.block is None:
            errors.append(f"{name}: {op.op} necesita el número de bloque.")
            continue
        low = 0 if op.op == "insert_after" else 1
        if not low <= op.block <= count:
            errors.append(
                f"{where}: no existe (la sección tiene {count} bloque{'s' if count != 1 else ''})."
            )
            continue
        if op.op == "insert_after":
            if op.text is None:
                errors.append(f"{where}: insert_after necesita el texto a insertar.")
                continue
            inserted.setdefault(op.block, []).append(op)
            continue
        if op.block in replaced:
            errors.append(f"{where}: hay más de una operación que lo sustituye o lo borra.")
            continue
        if op.op == "replace_block" and op.text is None:
            errors.append(f"{where}: replace_block necesita el texto nuevo.")
            continue
        replaced[op.block] = op

    result: list[Block] = []
    new_positions: set[int] = set()

    def add_new(blocks: list[Block]) -> None:
        for block in blocks:
            new_positions.add(len(result))
            result.append(block)

    for insert in inserted.get(0, []):
        add_new(_new_blocks(insert.text or "", f"{name}, bloque 0", errors))
    for number, block in enumerate(section.blocks, start=1):
        op = replaced.get(number)
        if op is None:
            result.append(block)
        elif op.op == "replace_block":
            blocks = _new_blocks(op.text or "", f"{name}, bloque {number}", errors)
            if blocks:
                blocks[-1] = blocks[-1].model_copy(update={"trailing": block.trailing})
            add_new(blocks)
        # delete_block: nothing
        for insert in inserted.get(number, []):
            add_new(_new_blocks(insert.text or "", f"{name}, bloque {number}", errors))
    return result, new_positions


def _add_footnotes(
    document: NotesDocument, footnotes: Sequence[NewFootnote], errors: list[str]
) -> NotesDocument:
    existing = {d.label: d.text.strip() for d in document.footnotes}
    lines: list[str] = []
    for footnote in footnotes:
        label = footnote.label.strip().removeprefix("[^").removesuffix("]")
        if not _LABEL.match(label):
            errors.append(
                f"La etiqueta de nota al pie «{footnote.label}» no es válida: usa letras, dígitos,"
                " «-» o «_»."
            )
            continue
        definition = footnote.definition.strip()
        if not definition or "\n" in definition:
            errors.append(f"La definición de [^{label}] debe ser una sola línea no vacía.")
            continue
        if label in existing:
            if existing[label] != definition:
                errors.append(
                    f"La nota al pie [^{label}] ya existe con otra definición"
                    f" («{existing[label]}»): usa otra etiqueta."
                )
            continue
        existing[label] = definition
        lines.append(NewFootnote(label=label, definition=definition).line())
    if not lines:
        return document
    addition = "\n".join(lines)
    holder: list[Block] = document.sections[-1].blocks if document.sections else document.preamble
    blocks = list(holder)
    if blocks and blocks[-1].kind == "footnotes":
        last = blocks[-1]
        blocks[-1] = last.model_copy(update={"text": f"{last.text}\n{addition}"})
    else:
        if blocks:
            blocks[-1] = _with_blank(blocks[-1])
        blocks.append(Block(kind="footnotes", text=addition, trailing="\n"))
    if document.sections:
        last_section = document.sections[-1].model_copy(update={"blocks": blocks})
        return document.model_copy(update={"sections": [*document.sections[:-1], last_section]})
    return document.model_copy(update={"preamble": blocks})


def apply_edits(notes: str, ops: Sequence[EditOp], footnotes: Sequence[NewFootnote] = ()) -> str:
    """The text of `notes` after `ops` and the new `footnotes`; nothing else changes.

    Raises:
        EditError: with every problem found (an unknown anchor, a block that does not exist, two
            ops on the same block, a heading inside a text, a clashing footnote label); nothing
            is applied then.
    """
    document = parse(notes)
    errors: list[str] = []
    anchors = {section.anchor for section in document.sections if section.anchor}
    by_section: dict[str, list[EditOp]] = {}
    for op in ops:
        anchor = op.section.strip().removeprefix("#")
        if anchor not in anchors:
            errors.append(f"No hay ninguna sección con el ancla #{anchor}.")
            continue
        by_section.setdefault(anchor, []).append(op)

    sections: list[Section] = []
    last_index = len(document.sections) - 1
    for index, section in enumerate(document.sections):
        section_ops = by_section.get(section.anchor or "")
        if not section_ops:
            sections.append(section)
            continue
        blocks, new = _rebuild_section(section, section_ops, errors)
        # A new block is separated from its neighbours by a blank line; the document still ends
        # in a single newline.
        for position in range(len(blocks)):
            if position == len(blocks) - 1 and index == last_index:
                if position in new:
                    blocks[position] = blocks[position].model_copy(update={"trailing": "\n"})
            elif position in new or position + 1 in new:
                blocks[position] = _with_blank(blocks[position])
        heading_trailing = section.heading_trailing
        if blocks and not heading_trailing.count("\n") >= 2:
            heading_trailing = "\n\n"
        sections.append(
            section.model_copy(update={"blocks": blocks, "heading_trailing": heading_trailing})
        )
    edited = document.model_copy(update={"sections": sections})
    edited = _add_footnotes(edited, footnotes, errors)
    if errors:
        raise EditError(errors)
    return serialize(edited)


def describe_sections(notes: str) -> str:
    """A numbered map of the notes' blocks, for the editor to address them: one line per block."""
    document = parse(notes)
    lines: list[str] = []
    for section in document.sections:
        lines.append(f"#{section.anchor} -- {section.heading.raw.strip()}")
        for number, block in enumerate(section.blocks, start=1):
            opening = " ".join(block.text.split())
            if len(opening) > 60:
                opening = opening[:60].rstrip() + "…"
            lines.append(f"  bloque {number} ({block.kind}): {opening}")
    return "\n".join(lines)


__all__ = [
    "EDIT_OP_NAMES",
    "EditError",
    "EditOp",
    "EditOpName",
    "NewFootnote",
    "apply_edits",
    "describe_sections",
]
