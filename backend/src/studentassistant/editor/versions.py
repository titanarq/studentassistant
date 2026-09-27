"""Notes versions: list the tagged versions of a topic's notes, diff two by section, restore one.

A notes version is the `<subject>/<topic>/apuntes-vN` tag `GitSync.create_notes_tag` puts on the
commit that wrote it ("prepárame el tema" makes one per generation). Everything here reads them
through `GitSync` (`list_notes_tags`, `read_file_at`), so the vault's git stays the vault's.

- `list_versions` -- every version, oldest first, with when it was made and whether the current
  `apuntes.md` is exactly that version.
- `read_version` -- the text of one version.
- `diff_versions` -- two versions (or a version and the current notes) compared **by section**:
  sections are matched by their stable anchor (ADR-0005), so a section whose title was renamed is
  still "changed", not removed and added; the preamble is a section of its own, and the footnote
  definitions ending the document are compared by label instead of showing up as a change of the
  last section.
- `restore_version` -- writes an older version back as `apuntes.md` and commits and tags it as the
  **next** version: nothing is rewound, so every version in between is still there.
- `mark_study_version` -- labels the current notes as the "versión de estudio" (#335): the latest
  version when the notes are exactly its text, else a new version tagged first. The label is
  recorded in `study/version.yaml` (the latest label plus the history of every label), written
  through the vault. A label, not a freeze: the notes stay editable, and `list_versions` says
  whether they still equal it (`study_current`).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from studentassistant.editor.inputs import fidelity_mode_of
from studentassistant.editor.notes_format import (
    Block,
    NotesDocument,
    notes_revision,
    parse,
    topic_source_resolver,
    validate,
)
from studentassistant.vault import (
    GitSync,
    NotesTag,
    Vault,
    get_topic,
    notes_path,
    read_notes,
    write_notes,
)
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.study import read_study_file, write_study_file

logger = logging.getLogger(__name__)

NOTES_RESTORED_KIND = "notes.restored"
PREAMBLE_KEY = ""
"""The `key` of the preamble (the `# title` and introduction) in a `SectionDiff`."""

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
SectionStatus = Literal["added", "removed", "changed", "unchanged"]


# -- errors ----------------------------------------------------------------------------------------


class VersionError(ValueError):
    """A versions request that cannot be done; the message is Spanish, for the student."""


class UnknownVersionError(VersionError):
    pass


class NothingToRestoreError(VersionError):
    pass


class NotesMissingError(VersionError):
    """The topic has no notes to label as the study version."""

    def __init__(self) -> None:
        super().__init__(
            "Todavía no hay apuntes de este tema: prepáralos antes de pasar a estudiar."
        )


# -- models ----------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotesVersion(_Strict):
    """One tagged version: `apuntes vN`."""

    version: int
    tag: str
    commit: str
    tagged_at: datetime | None = None
    message: str = ""
    current: bool = False
    """The current `apuntes.md` is exactly this version's text."""
    study: bool = False
    """This version was labelled "versión de estudio" at some point (`mark_study_version`)."""


class StudyLabel(VaultFileModel):
    """One "versión de estudio" label: which version, its notes' SHA-256 and when."""

    version: int
    tag: str
    notes_sha256: str
    marked_at: datetime


class StudyVersionFile(VaultFileModel):
    """`study/version.yaml`: the latest study label and every label, oldest first."""

    latest: StudyLabel
    history: list[StudyLabel]


class StudyVersion(_Strict):
    """What `mark_study_version` labelled."""

    version: int
    tag: str
    notes_sha256: str
    marked_at: datetime
    created_tag: bool
    """A new `apuntes-vN` tag was made for it (the notes had changed since the latest one)."""


class NotesVersions(_Strict):
    subject: str
    topic: str
    versions: list[NotesVersion]
    has_notes: bool
    """`apuntes.md` exists."""
    changed_since_latest: bool
    """`apuntes.md` exists and differs from the latest version (a revision, a doubt's edit)."""
    study_version: StudyLabel | None = None
    """The latest "versión de estudio" label; `None` before the first."""
    study_current: bool = False
    """`apuntes.md` exists and its SHA-256 equals the study label's (nothing edited since)."""


class VersionText(_Strict):
    subject: str
    topic: str
    version: int
    tag: str
    commit: str
    text: str


class SectionDiff(_Strict):
    """One section of either side, matched by anchor (`key`; the preamble is `PREAMBLE_KEY`)."""

    key: str
    anchor: str | None
    title_before: str | None
    title_after: str | None
    level: int
    status: SectionStatus
    moved: bool = False
    """Present on both sides, but in another place relative to the other common sections."""
    renamed: bool = False
    diff: str = ""
    """Unified diff of the section's text (heading and blocks, footnote definitions left out)."""


class FootnotesDiff(_Strict):
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []


class VersionDiff(_Strict):
    subject: str
    topic: str
    from_version: int
    to_version: int | None
    """`None`: the current `apuntes.md`."""
    identical: bool
    sections: list[SectionDiff]
    """In the order of the newer side, each removed section where it was."""
    footnotes: FootnotesDiff
    diff: str
    """Unified diff of the whole document."""


class RestoreResult(_Strict):
    subject: str
    topic: str
    restored_version: int
    version: int
    tag: str
    commit: str
    path: str
    diff: str
    """Unified diff from the notes before the restore to the restored ones."""
    notes: str
    revision: str | None = None
    """The revision (`notes_revision`) of `apuntes.md` after the restore."""
    errors: list[str] = []
    """What the provenance validator says of the restored text today (for example a source
    purged since): the restore is done anyway, since it is what the student asked for."""
    warning: str | None = None


# -- reading ---------------------------------------------------------------------------------------


def _relative_notes_path(vault: Vault, subject: str, topic: str) -> str:
    return notes_path(vault, subject, topic).relative_to(vault.path).as_posix()


def _tag_of(sync: GitSync, subject: str, topic: str, version: int) -> NotesTag:
    for tag in sync.list_notes_tags(subject, topic):
        if tag.version == version:
            return tag
    raise UnknownVersionError(f"No existe la versión {version} de los apuntes de este tema.")


def _text_at(vault: Vault, sync: GitSync, subject: str, topic: str, tag: NotesTag) -> str:
    text = sync.read_file_at(tag.commit, _relative_notes_path(vault, subject, topic))
    if text is None:
        raise UnknownVersionError(
            f"La versión {tag.version} de los apuntes no se encuentra en la bóveda."
        )
    return text


STUDY_VERSION_FILE = "version"
"""`study/version.yaml`: the study label of the topic."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_study_label(vault: Vault, subject: str, topic: str) -> StudyVersionFile | None:
    """The topic's `study/version.yaml`, or `None` before the first label (blocking)."""
    return read_study_file(vault, subject, topic, STUDY_VERSION_FILE, StudyVersionFile)


def study_current(label: StudyLabel | None, notes: str | None) -> bool:
    """Whether `notes` (the current `apuntes.md`) are still exactly the labelled ones."""
    return label is not None and notes is not None and _sha256(notes) == label.notes_sha256


def list_versions(vault: Vault, subject: str, topic: str, *, sync: GitSync) -> NotesVersions:
    """Every tagged version of the topic's notes, oldest first (blocking; reads only).

    Raises the vault's errors for an unknown topic.
    """
    get_topic(vault, subject, topic)
    current = read_notes(vault, subject, topic)
    label = read_study_label(vault, subject, topic)
    studied = {entry.tag for entry in label.history} if label is not None else set()
    relative = _relative_notes_path(vault, subject, topic)
    versions = []
    latest_text: str | None = None
    for tag in sync.list_notes_tags(subject, topic):
        latest_text = sync.read_file_at(tag.commit, relative)
        versions.append(
            NotesVersion(
                version=tag.version,
                tag=tag.name,
                commit=tag.commit,
                tagged_at=tag.tagged_at,
                message=tag.message,
                current=current is not None and latest_text == current,
                study=tag.name in studied,
            )
        )
    latest_label = label.latest if label is not None else None
    return NotesVersions(
        subject=subject,
        topic=topic,
        versions=versions,
        has_notes=current is not None,
        changed_since_latest=current is not None and bool(versions) and latest_text != current,
        study_version=latest_label,
        study_current=study_current(latest_label, current),
    )


def read_version(
    vault: Vault, subject: str, topic: str, version: int, *, sync: GitSync
) -> VersionText:
    """The text of version `version` (blocking). Raises `UnknownVersionError`."""
    get_topic(vault, subject, topic)
    tag = _tag_of(sync, subject, topic, version)
    return VersionText(
        subject=subject,
        topic=topic,
        version=version,
        tag=tag.name,
        commit=tag.commit,
        text=_text_at(vault, sync, subject, topic, tag),
    )


# -- diff ------------------------------------------------------------------------------------------


class _Part:
    """One comparable part of a document: the preamble or a section, without footnote blocks."""

    def __init__(self, key: str, anchor: str | None, title: str, level: int, text: str) -> None:
        self.key, self.anchor, self.title, self.level, self.text = key, anchor, title, level, text


def _render(blocks: list[Block]) -> str:
    return "".join(block.render() for block in blocks if block.kind != "footnotes")


def _parts(document: NotesDocument) -> list[_Part]:
    title = next(
        (b.text.removeprefix("#").strip() for b in document.preamble if b.kind == "title"), ""
    )
    parts = [_Part(PREAMBLE_KEY, None, title, 1, _render(document.preamble))]
    seen: set[str] = set()
    for index, section in enumerate(document.sections, start=1):
        key = section.anchor or f"sección-{index}"
        if key in seen:  # a repeated anchor: invalid notes, still compared
            key = f"{key}~{index}"
        seen.add(key)
        heading = section.heading.raw + section.heading_trailing
        parts.append(
            _Part(
                key,
                section.anchor,
                section.heading.title,
                section.heading.level,
                heading + _render(section.blocks),
            )
        )
    return parts


def _unified(before: str, after: str, old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
        )
    )


def _footnotes(before: NotesDocument, after: NotesDocument) -> FootnotesDiff:
    old = {d.label: d.text for d in before.footnotes}
    new = {d.label: d.text for d in after.footnotes}
    return FootnotesDiff(
        added=[label for label in new if label not in old],
        removed=[label for label in old if label not in new],
        changed=[label for label in new if label in old and old[label] != new[label]],
    )


def compare_notes(
    before: str, after: str, *, old_name: str = "antes", new_name: str = "después"
) -> tuple[list[SectionDiff], FootnotesDiff, str]:
    """Compare two notes texts by section (pure): the section diffs, footnotes and whole diff."""
    old_doc, new_doc = parse(before), parse(after)
    old_parts, new_parts = _parts(old_doc), _parts(new_doc)
    old_by_key = {part.key: part for part in old_parts}
    new_by_key = {part.key: part for part in new_parts}
    common_old = [p.key for p in old_parts if p.key in new_by_key]
    common_new = [p.key for p in new_parts if p.key in old_by_key]
    matcher = difflib.SequenceMatcher(a=common_old, b=common_new, autojunk=False)
    in_place = {
        key
        for block in matcher.get_matching_blocks()
        for key in common_old[block.a : block.a + block.size]
    }

    def entry(key: str) -> SectionDiff:
        old, new = old_by_key.get(key), new_by_key.get(key)
        if old is None and new is not None:
            status: SectionStatus = "added"
        elif new is None and old is not None:
            status = "removed"
        else:
            assert old is not None and new is not None
            status = "unchanged" if old.text == new.text else "changed"
        either = new or old
        assert either is not None
        name = f"#{key}" if key else "preámbulo"
        return SectionDiff(
            key=key,
            anchor=either.anchor,
            title_before=old.title if old else None,
            title_after=new.title if new else None,
            level=either.level,
            status=status,
            moved=old is not None and new is not None and key not in in_place,
            renamed=old is not None and new is not None and old.title != new.title,
            diff=_unified(
                old.text if old else "",
                new.text if new else "",
                f"{old_name} {name}",
                f"{new_name} {name}",
            ),
        )

    # The newer side's order, each removed section right after the part it followed.
    order = [p.key for p in new_parts]
    for index, part in enumerate(old_parts):
        if part.key in new_by_key:
            continue
        previous = next(
            (old_parts[i].key for i in range(index - 1, -1, -1) if old_parts[i].key in order),
            None,
        )
        order.insert(order.index(previous) + 1 if previous is not None else 0, part.key)
    sections = [entry(key) for key in order]
    return sections, _footnotes(old_doc, new_doc), _unified(before, after, old_name, new_name)


def diff_versions(
    vault: Vault,
    subject: str,
    topic: str,
    from_version: int,
    to_version: int | None = None,
    *,
    sync: GitSync,
) -> VersionDiff:
    """Version `from_version` against `to_version`, or against the current notes when `None`
    (blocking). Raises `UnknownVersionError`, or `VersionError` when there are no notes to
    compare with."""
    get_topic(vault, subject, topic)
    before = _text_at(vault, sync, subject, topic, _tag_of(sync, subject, topic, from_version))
    if to_version is None:
        after = read_notes(vault, subject, topic)
        if after is None:
            raise VersionError("Todavía no hay apuntes de este tema con los que comparar.")
        new_name = "apuntes actuales"
    else:
        after = _text_at(vault, sync, subject, topic, _tag_of(sync, subject, topic, to_version))
        new_name = f"apuntes v{to_version}"
    sections, footnotes, whole = compare_notes(
        before, after, old_name=f"apuntes v{from_version}", new_name=new_name
    )
    return VersionDiff(
        subject=subject,
        topic=topic,
        from_version=from_version,
        to_version=to_version,
        identical=before == after,
        sections=sections,
        footnotes=footnotes,
        diff=whole,
    )


# -- restore ---------------------------------------------------------------------------------------


def _restore(vault: Vault, sync: GitSync, subject: str, topic: str, version: int) -> RestoreResult:
    get_topic(vault, subject, topic)
    tag = _tag_of(sync, subject, topic, version)
    text = _text_at(vault, sync, subject, topic, tag)
    current = read_notes(vault, subject, topic)
    if current == text:
        raise NothingToRestoreError(
            f"Los apuntes actuales ya son los de la versión {version}: no hay nada que restaurar."
        )
    path = write_notes(vault, subject, topic, text)
    sync.note_change()
    existing = sync.list_notes_tags(subject, topic)
    next_version = (existing[-1].version if existing else 0) + 1
    message = f"Apuntes v{next_version} de {subject}/{topic}: restaurada la versión {version}"
    sync.checkpoint(message)
    new_tag = sync.create_notes_tag(subject, topic, message)
    mode = fidelity_mode_of(get_topic(vault, subject, topic).topic.fidelity_mode)
    errors = validate(text, mode, topic_source_resolver(vault, subject, topic))
    return RestoreResult(
        subject=subject,
        topic=topic,
        restored_version=version,
        version=new_tag.version,
        tag=new_tag.name,
        commit=new_tag.commit,
        path=path.relative_to(vault.path).as_posix(),
        diff=_unified(current or "", text, "apuntes.md (antes)", "apuntes.md"),
        notes=text,
        revision=notes_revision(text),
        errors=errors,
        warning=(
            "La versión restaurada no cumple hoy todas las reglas de procedencia (quizá cita una"
            " fuente que ya no está); revisa los avisos."
            if errors
            else None
        ),
    )


async def restore_version(
    vault: Vault,
    subject: str,
    topic: str,
    version: int,
    *,
    sync: GitSync,
    on_event: EventSink | None = None,
) -> RestoreResult:
    """Write version `version` back as `apuntes.md`, committed and tagged as the next version.

    No Claude call. `on_event("notes.restored", payload)` gets the result without the notes text.

    Raises:
        UnknownVersionError: no such version.
        NothingToRestoreError: the current notes already are that version; nothing written.
    """
    result = await asyncio.to_thread(_restore, vault, sync, subject, topic, version)
    if on_event is not None:
        payload = result.model_dump(mode="json", exclude={"notes"})
        try:
            await on_event(NOTES_RESTORED_KIND, payload)
        except Exception:
            logger.exception("could not publish %s for %s/%s", NOTES_RESTORED_KIND, subject, topic)
    return result


# -- study version ---------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def mark_study_version(
    vault: Vault,
    subject: str,
    topic: str,
    *,
    sync: GitSync,
    clock: Callable[[], datetime] = _utc_now,
) -> StudyVersion:
    """Label the current notes as the "versión de estudio" (blocking; no Claude call).

    When `apuntes.md` is exactly the latest `apuntes-vN` tag's text that version is labelled;
    otherwise the notes are committed and tagged as the next version first ("Apuntes vN de
    <s>/<t>: versión de estudio"). The label goes to `study/version.yaml` (latest + history) and
    is committed. Calling it again with unchanged notes returns the same label and writes nothing.
    Nothing locks the notes.

    Raises:
        NotesMissingError: the topic has no notes.
        The vault's errors for an unknown topic.
    """
    get_topic(vault, subject, topic)
    notes = read_notes(vault, subject, topic)
    if notes is None or not notes.strip():
        raise NotesMissingError
    sha = _sha256(notes)
    stored = read_study_label(vault, subject, topic)
    tags = sync.list_notes_tags(subject, topic)
    latest = tags[-1] if tags else None
    relative = _relative_notes_path(vault, subject, topic)
    tagged = latest is not None and sync.read_file_at(latest.commit, relative) == notes
    if (
        tagged
        and stored is not None
        and latest is not None
        and stored.latest.tag == latest.name
        and stored.latest.notes_sha256 == sha
    ):
        return StudyVersion(**stored.latest.model_dump(), created_tag=False)
    created = False
    if not tagged or latest is None:
        version = (latest.version if latest else 0) + 1
        message = f"Apuntes v{version} de {subject}/{topic}: versión de estudio"
        sync.note_change()
        sync.checkpoint(message)
        latest = sync.create_notes_tag(subject, topic, message)
        created = True
    label = StudyLabel(version=latest.version, tag=latest.name, notes_sha256=sha, marked_at=clock())
    history = [*(stored.history if stored is not None else []), label]
    write_study_file(
        vault,
        subject,
        topic,
        STUDY_VERSION_FILE,
        StudyVersionFile(latest=label, history=history),
    )
    sync.note_change()
    sync.checkpoint(
        f"Apuntes v{label.version} de {subject}/{topic}: marcada como versión de estudio"
    )
    return StudyVersion(**label.model_dump(), created_tag=created)


__all__ = [
    "NOTES_RESTORED_KIND",
    "PREAMBLE_KEY",
    "STUDY_VERSION_FILE",
    "FootnotesDiff",
    "NotesMissingError",
    "NothingToRestoreError",
    "NotesVersion",
    "NotesVersions",
    "RestoreResult",
    "SectionDiff",
    "StudyLabel",
    "StudyVersion",
    "StudyVersionFile",
    "UnknownVersionError",
    "VersionDiff",
    "VersionError",
    "VersionText",
    "compare_notes",
    "diff_versions",
    "list_versions",
    "mark_study_version",
    "read_study_label",
    "read_version",
    "restore_version",
    "study_current",
]
