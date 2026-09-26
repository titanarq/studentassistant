"""The student editing the notes directly: normalised, checked against the revision they started
from, validated, committed and told to the editor.

The web's document editor sends the whole `apuntes.md` the student ended up with and the revision
(`notes_format.notes_revision`) of the notes it started from. `save_student_edit`:

1. **Normalises** the text (`normalise_student_text`, pure): a section heading without anchor gets
   one (the slug of its title, made unique); every image block linking `../sources/images/...`
   gets the footnote of that pasted image (`[^img001]: [Imagen pegada 1](...)`); every other
   paragraph, list-item group or table with no footnote reference gets `[^est]` (`Escrito por el
   estudiante`, allowed in both fidelity modes); footnote definitions written anywhere but in the
   final run of definitions are moved to it.
2. **Validates** the result with `notes_format.validate` in the topic's fidelity mode; a failure
   is `StudentEditInvalidError` with the Spanish errors, and nothing is written.
3. Under the topic's short write lock (`notes_lock`), which the editor's turns take to apply their
   ops too, **compares** the current notes' revision with `base_revision`: when someone (the
   editor, another tab) changed them meanwhile it is `NotesChangedError`, carrying the current
   text and revision, and nothing is written; otherwise the notes are written
   (`vault.write_notes`) and committed at once (`Apuntes de <s>/<t> editados por el estudiante`).
4. **Records** a `student_edit` record in `conversations/editor.jsonl` (the changed sections and
   the diff), which the editor's next turn reads as part of the conversation, and sends
   `notes.edited` (origin `user`) to `on_event`.

Missing notes have no revision: a save with `base_revision=None` starts a document from nothing
(and is `NotesChangedError` once the topic has notes).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.inputs import fidelity_mode_of
from studentassistant.editor.notes_format import (
    CONTENT_KINDS,
    EST_LABEL,
    EST_TEXT,
    IMAGE_TEXT,
    LINK_PREFIX,
    Block,
    Heading,
    NotesDocument,
    Section,
    notes_revision,
    parse,
    serialize,
    topic_source_resolver,
    validate,
)
from studentassistant.editor.notes_lock import holding_notes
from studentassistant.editor.versions import PREAMBLE_KEY, compare_notes
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    get_topic,
    read_notes,
    write_notes,
)
from studentassistant.vault.slugs import slugify

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "editor"
STUDENT_EDIT_RECORD = "student_edit"
NOTES_EDITED_KIND = "notes.edited"
USER_ORIGIN = "user"
MAX_NOTES_CHARS = 400_000
"""The longest `apuntes.md` a student save accepts."""

Clock = Callable[[], datetime]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]

_IMAGE_LINK = re.compile(
    r"\]\(" + re.escape(LINK_PREFIX) + r"sources/images/img-(?P<number>\d{3,})\.(?:png|jpg|webp)\)"
)
_DEFINITION_LINE = re.compile(r"^\[\^([^\]\s]+)\]:")
_DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- errors ----------------------------------------------------------------------------------------


class StudentEditError(ValueError):
    """A student save that is not written; the message is Spanish."""


class NotesChangedError(StudentEditError):
    """The notes changed since the revision the student started from; nothing written."""

    def __init__(self, text: str, revision: str | None) -> None:
        super().__init__(
            "Los apuntes han cambiado mientras los editabas: revisa la versión actual y vuelve a"
            " guardar."
        )
        self.text = text
        self.revision = revision


class StudentEditInvalidError(StudentEditError):
    """The text breaks the notes format even after normalising; `errors` are Spanish."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# -- models ----------------------------------------------------------------------------------------


class StudentEditResult(BaseModel):
    """What one student save did; also the `student_edit` record and the event payload."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    topic: str
    revision: str = Field(description="The revision of `apuntes.md` after the save.")
    commit: str | None = None
    diff: str = Field(default="", description="Unified diff of notes/apuntes.md.")
    notes: str = Field(description="The notes as saved (normalised).")
    changed_sections: list[str] = Field(default_factory=list)
    normalised: bool = Field(description="True when the server changed the text it was sent.")
    notes_changed: bool = True


# -- normalisation (pure) -------------------------------------------------------------------------


def _anchor_for(title: str, taken: set[str]) -> str:
    try:
        base = slugify(title)
    except ValueError:
        base = "seccion"
    anchor, number = base, 2
    while anchor in taken:
        anchor = f"{base}-{number}"
        number += 1
    taken.add(anchor)
    return anchor


def _with_anchors(sections: list[Section]) -> list[Section]:
    taken = {section.anchor for section in sections if section.anchor}
    result: list[Section] = []
    for section in sections:
        heading = section.heading
        if heading.anchor is None:
            anchor = _anchor_for(heading.title, taken)
            raw = f"{heading.raw.rstrip()} {{#{anchor}}}"
            heading = Heading(raw=raw, level=heading.level, title=heading.title, anchor=anchor)
            section = section.model_copy(update={"heading": heading})
        result.append(section)
    return result


def _append_refs(block: Block, refs: str) -> Block:
    """`block` with the footnote references `refs` added where its kind keeps them."""
    lines = block.text.split("\n")
    if block.kind == "table":
        index = next(
            (i for i in range(len(lines) - 1, -1, -1) if not _DELIMITER_ROW.match(lines[i])),
            len(lines) - 1,
        )
        line = lines[index].rstrip()
        if line.endswith("|") and len(line) > 1:
            lines[index] = f"{line[:-1].rstrip()} {refs} |"
        else:
            lines[index] = line + refs
    else:
        lines[-1] = lines[-1].rstrip() + refs
    return block.model_copy(update={"text": "\n".join(lines)})


class _Footnotes:
    """The definitions the normalised notes have and the ones normalising adds."""

    def __init__(self, document: NotesDocument) -> None:
        self.defined = {d.label: d.text.strip() for d in document.footnotes}
        self.added: list[str] = []

    def add(self, label: str, definition: str) -> str:
        base, suffix = label, 2
        while label in self.defined and self.defined[label] != definition:
            label = f"{base}-{suffix}"
            suffix += 1
        if label not in self.defined:
            self.defined[label] = definition
            self.added.append(f"[^{label}]: {definition}")
        return label


def _cited_images(block: Block, footnotes: _Footnotes) -> set[int]:
    """The pasted images the block's footnotes already cite."""
    cited: set[int] = set()
    for label in block.footnote_refs:
        found = _IMAGE_LINK.search(footnotes.defined.get(label, ""))
        if found is not None:
            cited.add(int(found.group("number")))
    return cited


def _normalise_block(block: Block, footnotes: _Footnotes) -> Block:
    if block.kind not in CONTENT_KINDS:
        return block
    refs: list[str] = []
    cited = _cited_images(block, footnotes)
    for found in _IMAGE_LINK.finditer(block.text):
        number = int(found.group("number"))
        if number in cited:
            continue
        cited.add(number)
        link = found.group(0)[2:-1]
        definition = f"[{IMAGE_TEXT} {number}]({link})"
        refs.append(footnotes.add(f"img{number:03d}", definition))
    if not refs and not block.footnote_refs:
        refs.append(footnotes.add(EST_LABEL, EST_TEXT))
    if not refs:
        return block
    return _append_refs(block, "".join(f"[^{label}]" for label in refs))


def _stray_definitions(blocks: list[Block], keep_last: bool) -> tuple[list[Block], list[str]]:
    """`blocks` without their footnote runs (but the last when `keep_last`), and those lines."""
    kept: list[Block] = []
    moved: list[str] = []
    for index, block in enumerate(blocks):
        if block.kind != "footnotes" or (keep_last and index == len(blocks) - 1):
            kept.append(block)
            continue
        moved.extend(line for line in block.text.split("\n") if line.strip())
        if kept:
            kept[-1] = kept[-1].model_copy(update={"trailing": block.trailing})
    return kept, moved


def _with_blank(block: Block) -> Block:
    if block.trailing.count("\n") >= 2:
        return block
    return block.model_copy(update={"trailing": block.trailing.rstrip("\n") + "\n\n"})


def normalise_student_text(text: str) -> str:
    """The student's `apuntes.md` made to follow the notes format where it can be (pure).

    See the module docstring: anchors for sections without one, the pasted image's footnote for an
    image block, `[^est]` for any other content block citing nothing, and every footnote
    definition moved to the final run. What cannot be fixed this way (a reference to a footnote
    that is not defined, a repeated anchor) is left for `validate` to report. A text that needs
    none of it comes back unchanged, byte for byte.
    """
    document = parse(text)
    footnotes = _Footnotes(document)
    sections = _with_anchors(list(document.sections))
    preamble = [_normalise_block(block, footnotes) for block in document.preamble]
    sections = [
        section.model_copy(
            update={"blocks": [_normalise_block(block, footnotes) for block in section.blocks]}
        )
        for section in sections
    ]

    # Every footnote run but the document's last block is moved to the end.
    moved: list[str] = []
    preamble, lines = _stray_definitions(preamble, keep_last=not sections)
    moved.extend(lines)
    for index, section in enumerate(sections):
        blocks, lines = _stray_definitions(
            list(section.blocks), keep_last=index == len(sections) - 1
        )
        moved.extend(lines)
        sections[index] = section.model_copy(update={"blocks": blocks})
    addition = [*moved, *footnotes.added]
    if addition:
        holder = sections[-1].blocks if sections else preamble
        blocks = list(holder)
        if blocks and blocks[-1].kind == "footnotes":
            last = blocks[-1]
            joined = "\n".join([last.text, *addition])
            blocks[-1] = last.model_copy(update={"text": joined})
        else:
            if blocks:
                blocks[-1] = _with_blank(blocks[-1])
            elif sections:
                last_section = sections[-1]
                if last_section.heading_trailing.count("\n") < 2:
                    sections[-1] = last_section.model_copy(update={"heading_trailing": "\n\n"})
            blocks.append(Block(kind="footnotes", text="\n".join(addition), trailing="\n"))
        if sections:
            sections[-1] = sections[-1].model_copy(update={"blocks": blocks})
        else:
            preamble = blocks
    normalised = document.model_copy(update={"preamble": preamble, "sections": sections})
    return serialize(normalised)


# -- the save --------------------------------------------------------------------------------------


def _diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="apuntes.md (antes)",
            tofile="apuntes.md",
        )
    )


def _changed_sections(before: str, after: str) -> list[str]:
    sections, _footnotes, _diff_text = compare_notes(before, after)
    return [
        section.anchor or section.key
        for section in sections
        if section.key != PREAMBLE_KEY and (section.status != "unchanged" or section.moved)
    ]


def _save(
    vault: Vault,
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    text: str,
    base_revision: str | None,
) -> StudentEditResult:
    """Normalise, validate, compare and write; blocking (a worker thread)."""
    topic = get_topic(vault, subject_slug, topic_slug).topic
    if not text.strip():
        raise StudentEditInvalidError(["El documento está vacío: escribe algo antes de guardar."])
    if len(text) > MAX_NOTES_CHARS:
        raise StudentEditInvalidError(
            [f"El documento es demasiado largo (más de {MAX_NOTES_CHARS} caracteres)."]
        )
    normalised = normalise_student_text(text)
    mode = fidelity_mode_of(topic.fidelity_mode)
    with holding_notes(vault, subject_slug, topic_slug):
        stored = read_notes(vault, subject_slug, topic_slug)
        revision = None if stored is None else notes_revision(stored)
        current = stored or ""
        if revision != base_revision:
            raise NotesChangedError(current, revision)
        errors = validate(normalised, mode, topic_source_resolver(vault, subject_slug, topic_slug))
        if errors:
            raise StudentEditInvalidError(errors)
        changed = normalised != current
        commit = None
        if changed:
            write_notes(vault, subject_slug, topic_slug, normalised)
            sync.note_change()
            commit = sync.checkpoint(
                f"Apuntes de {subject_slug}/{topic_slug} editados por el estudiante"
            )
    return StudentEditResult(
        subject=subject_slug,
        topic=topic_slug,
        revision=notes_revision(normalised),
        commit=commit,
        diff=_diff(current, normalised) if changed else "",
        notes=normalised,
        changed_sections=_changed_sections(current, normalised) if changed else [],
        normalised=normalised != text,
        notes_changed=changed,
    )


async def save_student_edit(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    text: str,
    base_revision: str | None,
    *,
    sync: GitSync,
    on_event: EventSink | None = None,
    clock: Clock = _utc_now,
) -> StudentEditResult:
    """Save the student's whole `apuntes.md` (see the module docstring).

    Raises:
        NotesChangedError: `base_revision` is not the current notes' revision (`None`: the topic
            has no notes); nothing written.
        StudentEditInvalidError: the normalised text does not pass `validate`; nothing written.
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
    """
    result = await asyncio.to_thread(
        _save, vault, sync, subject_slug, topic_slug, text, base_revision
    )
    if not result.notes_changed:
        return result
    payload = result.model_dump(mode="json", exclude={"notes"})
    entry = ConversationRecord(time=clock(), kind=STUDENT_EDIT_RECORD, detail=payload)
    try:
        await asyncio.to_thread(
            append_conversation_record, vault, subject_slug, topic_slug, CONVERSATION_NAME, entry
        )
    except Exception:
        logger.exception("could not record the student's edit of %s/%s", subject_slug, topic_slug)
    sync.note_change()
    if on_event is not None:
        try:
            await on_event(NOTES_EDITED_KIND, {**payload, "origin": USER_ORIGIN})
        except Exception:
            logger.exception(
                "could not publish %s for %s/%s", NOTES_EDITED_KIND, subject_slug, topic_slug
            )
    return result


__all__ = [
    "NOTES_EDITED_KIND",
    "STUDENT_EDIT_RECORD",
    "NotesChangedError",
    "StudentEditError",
    "StudentEditInvalidError",
    "StudentEditResult",
    "normalise_student_text",
    "save_student_edit",
]
