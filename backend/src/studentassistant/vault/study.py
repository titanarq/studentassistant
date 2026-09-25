"""The student's study history: `study/<name>.jsonl` under a topic, one record per line.

What the student does with the generated material -- a quiz taken (`study/quiz-results.jsonl`),
later flashcard reviews -- is kept in the vault like everything else, so it travels with it to
another PC. This module stores whatever record model a feature module hands it and knows nothing
about quizzes: the file is append-only JSONL (`append_jsonl`: secret guard, fsynced, a torn last
line ignored on read) and `*.jsonl` merges with `merge=union`, so two PCs' lines are both kept.
Nothing here runs git.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from studentassistant.vault.errors import VaultError
from studentassistant.vault.jsonl import append_jsonl, read_jsonl
from studentassistant.vault.topics import get_topic, topic_directory
from studentassistant.vault.vault import Vault

STUDY_DIRNAME = "study"
STUDY_LOG_SUFFIX = ".jsonl"
# `quiz-results`: lowercase ASCII, digits and hyphens, no path separator.
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


class StudyLogError(VaultError):
    """A study log name this backend refuses (it would not be a plain file name)."""


def study_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where a topic's study history lives, whether or not anything was written yet."""
    return topic_directory(vault, subject_slug, topic_slug) / STUDY_DIRNAME


def study_log_path(vault: Vault, subject_slug: str, topic_slug: str, name: str) -> Path:
    """The file of study log `name` (`study/<name>.jsonl`) of a topic.

    Raises:
        StudyLogError: `name` is not a plain log name.
    """
    if not _NAME.fullmatch(name):
        raise StudyLogError(f"{name!r} is not a study log name (lowercase letters, digits, -)")
    return study_directory(vault, subject_slug, topic_slug) / f"{name}{STUDY_LOG_SUFFIX}"


def append_study_record(
    vault: Vault, subject_slug: str, topic_slug: str, name: str, record: BaseModel
) -> Path:
    """Append `record` to study log `name` of the topic, creating it on first use.

    Returns the log's path.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: the topic is
            unknown or unreadable.
        StudyLogError: `name` is not a plain log name.
        SecretRefused: the line looks like it carries a secret; nothing is written.
        OSError: the file cannot be written.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = study_log_path(vault, subject_slug, topic_slug, name)
    path.parent.mkdir(exist_ok=True)
    append_jsonl(path, record)
    return path


def read_study_records[T: BaseModel](
    vault: Vault, subject_slug: str, topic_slug: str, name: str, model: type[T]
) -> list[T]:
    """Every complete record of study log `name`, in file order; empty when there is none.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: the topic is
            unknown or unreadable.
        StudyLogError: `name` is not a plain log name.
        JsonlError: a complete line is not JSON or not a `model`.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = study_log_path(vault, subject_slug, topic_slug, name)
    if not path.exists():
        return []
    return list(read_jsonl(path, model))
