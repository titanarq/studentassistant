"""Notes and generated material: `notes/apuntes.md`, its draft, and listing `generated/`.

The master notes are written by the editor through `write_notes` (and a generation that did not
pass the provenance validator through `write_notes_draft`, to `notes/borrador.md`, so the last
valid `apuntes.md` is never overwritten by an invalid one); the files under `generated/` by the
generators, through `write_generated` / `remove_generated` (names checked by
`check_generated_name`). The readers answer "nothing yet" (`None`, an empty list) for a topic that
has neither. Nothing here runs git: committing and tagging a notes
version is `GitSync`'s job.
"""

from __future__ import annotations

import re
from pathlib import Path

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import write_bytes_atomic, write_text_atomic
from studentassistant.vault.topics import require_topic, topic_directory
from studentassistant.vault.vault import Vault

NOTES_DIRNAME = "notes"
NOTES_FILE_NAME = "apuntes.md"
DRAFT_FILE_NAME = "borrador.md"
GENERATED_DIRNAME = "generated"

# One segment of a name under `generated/`: no leading dot (so no `..`, no hidden file).
_GENERATED_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class NotesError(VaultError):
    """A topic's notes this backend cannot read; the message says why."""


def notes_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The path of a topic's master notes, whether or not they were written."""
    return topic_directory(vault, subject_slug, topic_slug) / NOTES_DIRNAME / NOTES_FILE_NAME


def notes_draft_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The path of a topic's notes draft (`notes/borrador.md`), whether or not it was written."""
    return topic_directory(vault, subject_slug, topic_slug) / NOTES_DIRNAME / DRAFT_FILE_NAME


def generated_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where a topic's generated material lives, whether or not it has any yet."""
    return topic_directory(vault, subject_slug, topic_slug) / GENERATED_DIRNAME


def read_notes(vault: Vault, subject_slug: str, topic_slug: str) -> str | None:
    """The text of the topic's `notes/apuntes.md`, or `None` when it has not been written yet.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read (a value that is not a slug is not found).
        NotesError: when the file exists but is a symlink, or cannot be read as UTF-8.
    """
    require_topic(vault, subject_slug, topic_slug)
    path = notes_path(vault, subject_slug, topic_slug)
    if path.is_symlink():
        raise NotesError(f"{path} is a symlink; the notes are a file of the vault")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise NotesError(f"{path} cannot be read: {error}") from error


def read_notes_draft(vault: Vault, subject_slug: str, topic_slug: str) -> str | None:
    """The text of the topic's `notes/borrador.md`, or `None` when there is none.

    Raises the same errors as `read_notes`.
    """
    require_topic(vault, subject_slug, topic_slug)
    return _read(notes_draft_path(vault, subject_slug, topic_slug))


def write_notes(vault: Vault, subject_slug: str, topic_slug: str, text: str) -> Path:
    """Write `text` as the topic's `notes/apuntes.md` (atomically, creating `notes/`).

    Any draft left by an earlier generation (`notes/borrador.md`) is removed: the notes it was a
    failed attempt at are now written. Returns the path written.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read; nothing is written.
        NotesError: when `notes/apuntes.md` or `notes/` is a symlink.
        SecretRefused: when the text looks like it carries a key; nothing is written.
    """
    path = _prepare(vault, subject_slug, topic_slug, notes_path(vault, subject_slug, topic_slug))
    write_text_atomic(path, text)
    draft = notes_draft_path(vault, subject_slug, topic_slug)
    if draft.is_file() and not draft.is_symlink():
        draft.unlink()
    return path


def write_notes_draft(vault: Vault, subject_slug: str, topic_slug: str, text: str) -> Path:
    """Write `text` as the topic's `notes/borrador.md`, leaving `notes/apuntes.md` untouched.

    Raises the same errors as `write_notes`. Returns the path written.
    """
    path = notes_draft_path(vault, subject_slug, topic_slug)
    path = _prepare(vault, subject_slug, topic_slug, path)
    write_text_atomic(path, text)
    return path


def _prepare(vault: Vault, subject_slug: str, topic_slug: str, path: Path) -> Path:
    require_topic(vault, subject_slug, topic_slug)
    if path.parent.is_symlink() or path.is_symlink():
        raise NotesError(f"{path} is a symlink; the notes are a file of the vault")
    path.parent.mkdir(exist_ok=True)
    return path


def _read(path: Path) -> str | None:
    if path.is_symlink() or path.parent.is_symlink():
        raise NotesError(f"{path} is a symlink; the notes are a file of the vault")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise NotesError(f"{path} cannot be read: {error}") from error


def list_generated(vault: Vault, subject_slug: str, topic_slug: str) -> list[str]:
    """The vault-relative POSIX paths of every file under the topic's `generated/`, sorted.

    Subdirectories are walked; symlinks (files or directories) are never listed or followed. A
    topic without `generated/` lists as empty.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read (a value that is not a slug is not found).
    """
    require_topic(vault, subject_slug, topic_slug)
    root = generated_directory(vault, subject_slug, topic_slug)
    if root.is_symlink() or not root.is_dir():
        return []
    found: list[str] = []
    for directory, subdirectories, files in root.walk():
        subdirectories[:] = [d for d in subdirectories if not (directory / d).is_symlink()]
        for name in files:
            path = directory / name
            if not path.is_symlink() and path.is_file():
                found.append(path.relative_to(vault.path).as_posix())
    return sorted(found)


def check_generated_name(name: str) -> None:
    """Refuse a name under `generated/` that is not relative segments of `[A-Za-z0-9._-]`.

    Raises:
        NotesError: `name` is empty, absolute, has an empty, dotted or unusual segment.
    """
    segments = name.split("/")
    if not name or not all(_GENERATED_SEGMENT.fullmatch(segment) for segment in segments):
        raise NotesError(f"{name!r} is not a file name under generated/")


def _generated_file(vault: Vault, subject_slug: str, topic_slug: str, name: str) -> Path:
    check_generated_name(name)
    require_topic(vault, subject_slug, topic_slug)
    root = generated_directory(vault, subject_slug, topic_slug)
    path = root.joinpath(*name.split("/"))
    current = path
    while True:
        if current.is_symlink():
            raise NotesError(f"{current} is a symlink; generated material is a file of the vault")
        if current == root:
            return path
        current = current.parent


def write_generated(
    vault: Vault, subject_slug: str, topic_slug: str, name: str, content: str | bytes
) -> Path:
    """Write `content` as `generated/<name>` of the topic (atomically, creating directories).

    `name` is relative to `generated/` and may hold subdirectories (`a/b.md`). Text is written as
    UTF-8. Returns the path written.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read; nothing is written.
        NotesError: `name` is not a generated file name, or a directory on the way is a symlink.
        SecretRefused: when the content looks like it carries a key; nothing is written.
    """
    path = _generated_file(vault, subject_slug, topic_slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    write_bytes_atomic(path, data)
    return path


def read_generated(vault: Vault, subject_slug: str, topic_slug: str, name: str) -> bytes | None:
    """The bytes of `generated/<name>`, or `None` when there is no such file.

    Raises the same errors as `write_generated` (but `SecretRefused`).
    """
    path = _generated_file(vault, subject_slug, topic_slug, name)
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError as error:
        raise NotesError(f"{path} cannot be read: {error}") from error


def remove_generated(vault: Vault, subject_slug: str, topic_slug: str, name: str) -> bool:
    """Remove `generated/<name>`; `False` when there was no such file. Empty directories left
    behind under `generated/` are removed too.

    Raises the same errors as `read_generated`.
    """
    path = _generated_file(vault, subject_slug, topic_slug, name)
    if not path.is_file():
        return False
    path.unlink()
    root = generated_directory(vault, subject_slug, topic_slug)
    parent = path.parent
    while parent != root and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return True
