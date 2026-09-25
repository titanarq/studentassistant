"""LLM role conversations: `conversations/<name>.jsonl` under a topic, one record per line.

ADR-0003 persists each role's conversation so it can be inspected, audited and resumed on another
PC; ADR-0002 makes the vault its only writer. This module stores what a role hands it and knows
nothing about Claude: a record is the time it was written, a `kind` the role chooses (the observer
uses `context`, `user`, `assistant` and `status`), optionally the API `message` it carries, and for
an answer the `model`, `prompt_hash` and token `usage` of the call that produced it.

The file is append-only JSONL (`append_jsonl`): every line goes through the secret guard, is on
disk once the append returns, and a torn last line is ignored on read. `*.jsonl` merges with
`merge=union`, so two PCs' lines are both kept. Nothing here runs git.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from studentassistant.vault.errors import VaultError
from studentassistant.vault.jsonl import append_jsonl, read_jsonl
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.topics import get_topic, topic_directory
from studentassistant.vault.vault import Vault

CONVERSATIONS_DIRNAME = "conversations"
CONVERSATION_SUFFIX = ".jsonl"
# `observer-20260925-180000`, `editor`: lowercase ASCII, digits and hyphens, no path separator.
CONVERSATION_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,99}$"
_NAME = re.compile(CONVERSATION_NAME_PATTERN)


class ConversationError(VaultError):
    """A conversation name this backend refuses (it would not be a plain file name)."""


class ConversationRecord(VaultFileModel):
    """One line of a conversation file."""

    time: datetime
    kind: str = Field(min_length=1, max_length=40)
    message: dict[str, Any] | None = None
    model: str | None = None
    prompt_hash: str | None = None
    usage: dict[str, int] | None = None
    detail: dict[str, Any] | None = None

    @field_validator("time")
    @classmethod
    def aware_utc(cls, time: datetime) -> datetime:
        if time.tzinfo is None or time.utcoffset() is None:
            raise ValueError("a conversation record time must be timezone-aware")
        return time.astimezone(UTC)


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ConversationError(
            f"{name!r} is not a conversation name (lowercase letters, digits and hyphens)"
        )


def conversations_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where a topic's conversations live, whether or not one has been written yet."""
    return topic_directory(vault, subject_slug, topic_slug) / CONVERSATIONS_DIRNAME


def conversation_path(vault: Vault, subject_slug: str, topic_slug: str, name: str) -> Path:
    """The file of conversation `name` of a topic.

    Raises:
        ConversationError: `name` is not a plain conversation name.
    """
    _check_name(name)
    return conversations_directory(vault, subject_slug, topic_slug) / f"{name}{CONVERSATION_SUFFIX}"


def append_conversation_record(
    vault: Vault, subject_slug: str, topic_slug: str, name: str, record: ConversationRecord
) -> None:
    """Append `record` to conversation `name` of the topic, creating the file on first use.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: the topic is
            unknown or unreadable.
        ConversationError: `name` is not a plain conversation name.
        SecretRefused: the line looks like it carries a secret; nothing is written.
        OSError: the file cannot be written.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = conversation_path(vault, subject_slug, topic_slug, name)
    path.parent.mkdir(exist_ok=True)
    append_jsonl(path, record)


def read_conversation(
    vault: Vault, subject_slug: str, topic_slug: str, name: str
) -> list[ConversationRecord]:
    """Every complete record of conversation `name`, in file order; empty when there is none.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: the topic is
            unknown or unreadable.
        ConversationError: `name` is not a plain conversation name.
        JsonlError: a complete line is not JSON or not a `ConversationRecord`.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = conversation_path(vault, subject_slug, topic_slug, name)
    if not path.exists():
        return []
    return list(read_jsonl(path, ConversationRecord))
