"""Notes and generated material: reading `notes/apuntes.md` and listing `generated/`.

Only reading lives here. The master notes are written by the editor and the files under
`generated/` by the generators, through writers of their own tasks; until then these readers
answer "nothing yet" (`None`, an empty list) for a topic that has neither. Nothing here writes a
file or runs git.
"""

from __future__ import annotations

from pathlib import Path

from studentassistant.vault.errors import VaultError
from studentassistant.vault.topics import require_topic, topic_directory
from studentassistant.vault.vault import Vault

NOTES_DIRNAME = "notes"
NOTES_FILE_NAME = "apuntes.md"
GENERATED_DIRNAME = "generated"


class NotesError(VaultError):
    """A topic's notes this backend cannot read; the message says why."""


def notes_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The path of a topic's master notes, whether or not they were written."""
    return topic_directory(vault, subject_slug, topic_slug) / NOTES_DIRNAME / NOTES_FILE_NAME


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
