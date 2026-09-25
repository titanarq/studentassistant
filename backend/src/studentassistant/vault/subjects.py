"""Subjects: the first level of the vault layout, one directory per school subject.

A subject is the drawer everything else of a course goes into -- its topics, and inside them the
sources, the sessions, the notes and the generated material of `docs/modules/vault.md` -- so what
this module writes is small (`subjects/<slug>/subject.yaml`: the name the student calls it by and
the editor's preferences for it) and what it decides is not: the slug it picks becomes a directory
name that later content is written under and that the student reads in the vault's diffs on GitHub.

Two rules follow from that. The slug is derived from the Spanish name and never reused as an
identity of its own, so the name in `subject.yaml` stays what the UI shows. And a name whose slug
another subject already has gets a numeric suffix instead of the other subject's directory: two
subjects that sound alike -- "Inglés" and one typed without its accent -- are two drawers, and
silently writing the second one's preferences over the first one's would lose content the vault is
the source of truth for (ADR-0002).

Reading is as strict as `Vault.open` is about `vault.yaml`. A directory under `subjects/` whose
`subject.yaml` is missing or unreadable is refused with an error that names the file, rather than
left out of the listing: a subject that quietly disappears from `list_subjects` is a subject the
student cannot open any more, and the vault holds the only copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.files import read_yaml, write_yaml_atomic
from studentassistant.vault.models import Subject
from studentassistant.vault.slugs import is_slug, slugify, unique_slug
from studentassistant.vault.vault import Vault, VaultError

SUBJECTS_DIRNAME = "subjects"
SUBJECT_FILE_NAME = "subject.yaml"


class SubjectError(VaultError):
    """A subject this backend cannot create, find or read; the message says which and why."""


class SubjectNotFoundError(SubjectError):
    """There is no subject with that slug in this vault."""


class SubjectFileError(SubjectError):
    """`subject.yaml` is missing, or holds something this backend cannot read as a `Subject`."""


@dataclass(frozen=True)
class StoredSubject:
    """A subject as the vault holds it: the slug naming its directory and its `subject.yaml`."""

    slug: str
    subject: Subject


def subject_directory(vault: Vault, slug: str) -> Path:
    """The directory the subject called `slug` lives in, whether or not it exists yet."""
    return _subjects_root(vault) / slug


def create_subject(vault: Vault, name: str, style_guide: str | None = None) -> StoredSubject:
    """Create `subjects/<slug>/subject.yaml` for the subject the student calls `name`.

    The slug is `slugify(name)`, suffixed with the first free number when another subject already
    has it, so the new subject gets a directory of its own. `style_guide` is what the tutor-editor
    is asked to respect for this subject -- how it names things, how formal it is, which notation
    the course uses -- and stays `None` until the student has one to give.

    Raises:
        ValueError: when `name` holds no letter and no digit, so there is no slug to name the
            directory after; nothing is written.
        FileExistsError: when the directory of the slug that was picked is there already, which is
            what creating the same subject twice at once looks like; nothing is overwritten.
        OSError: when the directory cannot be made or the file cannot be written.
    """
    slug = unique_slug(slugify(name), _taken_slugs(vault))
    directory = subject_directory(vault, slug)
    directory.mkdir(parents=True)
    subject = Subject(name=name, style_guide=style_guide)
    write_yaml_atomic(directory / SUBJECT_FILE_NAME, subject)
    return StoredSubject(slug=slug, subject=subject)


def list_subjects(vault: Vault) -> list[StoredSubject]:
    """Every subject of the vault, sorted by slug, so a listing never depends on the file system.

    A vault that has no subject yet has no `subjects/` directory either, and lists as empty.

    Raises:
        SubjectFileError: when a directory under `subjects/` has no `subject.yaml` this backend can
            read as a `Subject`; the subject stays in the listing as an error instead of vanishing.
    """
    return [get_subject(vault, slug) for slug in sorted(_taken_slugs(vault))]


def get_subject(vault: Vault, slug: str) -> StoredSubject:
    """Read the subject called `slug`, which is the whole of `subjects/<slug>/subject.yaml`.

    Raises:
        SubjectNotFoundError: when the vault has no such subject directory.
        SubjectFileError: when the directory is there but its `subject.yaml` is missing,
            unreadable, not YAML, or holds fields that are not a `Subject`.
    """
    directory = subject_directory(vault, slug)
    if not directory.is_dir():
        raise SubjectNotFoundError(
            f"the vault at {vault.path} has no subject {slug!r}: {directory} is not a directory"
        )
    return StoredSubject(slug=slug, subject=_read_subject_file(directory / SUBJECT_FILE_NAME))


def set_style_guide(vault: Vault, slug: str, style_guide: str | None) -> StoredSubject:
    """Replace the subject's `style_guide` in its `subject.yaml` (`None` or blank clears it).

    Every other field is kept as read; nothing is rewritten when the guide is the same.

    Raises:
        SubjectNotFoundError, SubjectFileError: as `get_subject` (a value that is not a slug is
            not found).
        OSError: the file cannot be written.
    """
    if not is_slug(slug):
        raise SubjectNotFoundError(f"{slug!r} is not a subject slug")
    stored = get_subject(vault, slug)
    guide = style_guide if style_guide and style_guide.strip() else None
    if stored.subject.style_guide == guide:
        return stored
    subject = stored.subject.model_copy(update={"style_guide": guide})
    write_yaml_atomic(subject_directory(vault, slug) / SUBJECT_FILE_NAME, subject)
    return StoredSubject(slug=slug, subject=subject)


def _subjects_root(vault: Vault) -> Path:
    """Where this vault keeps its subjects; it does not exist until the first one is created."""
    return vault.path / SUBJECTS_DIRNAME


def _taken_slugs(vault: Vault) -> list[str]:
    """The slugs already in use under `subjects/`, so a new one cannot be given to a second one.

    Every directory counts, including one whose `subject.yaml` is missing: a half-written subject
    is still a subject the student may recover, and a new one taking its slug would write over it.
    """
    root = _subjects_root(vault)
    if not root.is_dir():
        return []
    return [entry.name for entry in root.iterdir() if entry.is_dir()]


def _read_subject_file(subject_path: Path) -> Subject:
    """Read `subject.yaml`, naming in the error which of the ways it can be wrong it is.

    Raises:
        SubjectFileError: when the file is missing, unreadable, not YAML or not a `Subject`.
    """
    try:
        return read_yaml(subject_path, Subject)
    except FileNotFoundError as error:
        raise SubjectFileError(
            f"{subject_path} is missing, so {subject_path.parent} does not hold a subject"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise SubjectFileError(f"{subject_path} cannot be read: {error}") from error
    except YAMLError as error:
        raise SubjectFileError(
            f"{subject_path} is not YAML this backend can parse: {error}"
        ) from error
    except ValidationError as error:
        raise SubjectFileError(f"{subject_path} does not hold a subject: {error}") from error
