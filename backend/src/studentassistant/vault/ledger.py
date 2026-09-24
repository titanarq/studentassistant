"""The LLM cost ledger: one `ledger.jsonl` per topic, a line per Claude call made for it.

The llm module prices each call and enforces the caps (ADR-0004); this module only stores what it
is handed, because the vault is written only through `studentassistant.vault` (ADR-0002) and the
vault never imports a feature module. A line is appended with `append_jsonl`, so it goes through
the secret guard, is on disk once the append returns, and a torn last line a crash left behind is
ignored on read and cut away on the next append. Nothing here runs git.

The ledger lives under its topic directory, so an entry names the subject and the topic it was
spent on and the append refuses an entry whose `subject`/`topic` are not the ones it is written
under: a reader summing the whole vault reads those fields, not the path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, field_validator

from studentassistant.vault.errors import VaultError
from studentassistant.vault.jsonl import append_jsonl, read_jsonl
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.session_models import SESSION_ID_PATTERN
from studentassistant.vault.subjects import list_subjects
from studentassistant.vault.topics import get_topic, list_topics, topic_directory
from studentassistant.vault.vault import Vault

LEDGER_FILE_NAME = "ledger.jsonl"


class LedgerError(VaultError):
    """A ledger entry this backend refuses to write where it was asked to."""


class LedgerEntry(VaultFileModel):
    """One line of `ledger.jsonl`: the usage of one LLM call and, when priced, what it cost.

    `estimated_usd` is `None` when the model has no known price; the token counts are always there.
    """

    time: datetime
    role: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_hash: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_usd: float | None = Field(default=None, ge=0)
    subject: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    session: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)

    @field_validator("time")
    @classmethod
    def aware_utc(cls, time: datetime) -> datetime:
        if time.tzinfo is None or time.utcoffset() is None:
            raise ValueError("a ledger time must be timezone-aware")
        return time.astimezone(UTC)


def ledger_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where the ledger of a topic lives, whether or not anything has been appended to it yet."""
    return topic_directory(vault, subject_slug, topic_slug) / LEDGER_FILE_NAME


def append_ledger_entry(
    vault: Vault, subject_slug: str, topic_slug: str, entry: LedgerEntry
) -> None:
    """Append `entry` as one line of the topic's `ledger.jsonl`, creating the file on first use.

    Raises:
        SubjectNotFoundError, SubjectFileError: when the subject is unknown or unreadable.
        TopicNotFoundError, TopicFileError: when the topic is unknown or unreadable.
        LedgerError: when `entry.subject`/`entry.topic` are not `subject_slug`/`topic_slug`.
        SecretRefused: when the line looks like it carries a secret; nothing is written.
        OSError: when the file cannot be written.
    """
    get_topic(vault, subject_slug, topic_slug)
    if (entry.subject, entry.topic) != (subject_slug, topic_slug):
        raise LedgerError(
            f"a ledger entry of {entry.subject!r}/{entry.topic!r} cannot be written to the ledger"
            f" of {subject_slug!r}/{topic_slug!r}"
        )
    append_jsonl(ledger_path(vault, subject_slug, topic_slug), entry)


def read_ledger(vault: Vault, subject_slug: str, topic_slug: str) -> list[LedgerEntry]:
    """Every complete entry of the topic's ledger in file order; empty when there is no ledger.

    Raises:
        SubjectNotFoundError, SubjectFileError: when the subject is unknown or unreadable.
        TopicNotFoundError, TopicFileError: when the topic is unknown or unreadable.
        JsonlError: when a complete line is not JSON or not a `LedgerEntry`.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = ledger_path(vault, subject_slug, topic_slug)
    if not path.exists():
        return []
    return list(read_jsonl(path, LedgerEntry))


def read_all_ledgers(vault: Vault) -> Iterator[LedgerEntry]:
    """Every entry of every topic of every subject: subjects and topics by slug, lines in order.

    Raises the errors of `list_subjects`, `list_topics` and `read_ledger`.
    """
    for subject in list_subjects(vault):
        for topic in list_topics(vault, subject.slug):
            yield from read_ledger(vault, subject.slug, topic.slug)
