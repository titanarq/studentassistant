"""The feedback inbox: bugs and improvements of the app itself the student reports in a chat (#472).

A chat turn that recognises app feedback («apunta una mejora: ...», «esto es un bug: ...») records
one item here; the maintainer reads the inbox with `studentassistant feedback list` and triages each
item into an issue of the code repository by hand -- the backend never calls GitHub (the code
repository is public, the vault is the student's private content).

The inbox is `feedback/inbox.jsonl` at the vault root, append-only JSONL like every other log
(`append_jsonl`: secret guard, fsynced; `*.jsonl` merges with `union`). Two kinds of line:

- `{"record": "item", ...}` -- a new item: `id` (`fb-N`, one past the highest in the file),
  `created_at`, `kind` (`bug` | `mejora`), `title`, `body`, `context`;
- `{"record": "status", "id", "time", "status", "issue"}` -- a later status change (`nuevo`,
  `triado`, `descartado`) with its triage reference (an issue number, or `None`).

Reading folds the lines in file order: an item's status is its last change (`nuevo` without one).
Every write -- allocating the id and appending, or checking the id and appending a change -- runs
under the cross-process vault lock `feedback` (`locking.py`), so the server and the CLI never give
two items one id. Nothing here runs git: the caller commits (the sync loop, or a checkpoint).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator

from studentassistant.vault.errors import VaultError
from studentassistant.vault.jsonl import append_jsonl, read_jsonl
from studentassistant.vault.locking import vault_lock
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.vault import Vault

FEEDBACK_DIRNAME = "feedback"
INBOX_FILENAME = "inbox.jsonl"
FEEDBACK_LOCK_NAME = "feedback"
FEEDBACK_LOCK_TIMEOUT_SECONDS = 30.0
MAX_TITLE_CHARS = 140
MAX_BODY_CHARS = 4000
MAX_EXCERPT_CHARS = 1200
FEEDBACK_ID_PATTERN = r"^fb-[1-9][0-9]*$"
_ID = re.compile(FEEDBACK_ID_PATTERN)

FeedbackKind = Literal["bug", "mejora"]
FeedbackStatus = Literal["nuevo", "triado", "descartado"]
FeedbackMode = Literal["construir", "estudiar"]
FEEDBACK_KINDS: tuple[FeedbackKind, ...] = ("bug", "mejora")
FEEDBACK_STATUSES: tuple[FeedbackStatus, ...] = ("nuevo", "triado", "descartado")


class FeedbackError(VaultError):
    """A feedback write the inbox refuses."""


class FeedbackNotFoundError(FeedbackError):
    """No item of the inbox has that id."""


def _aware(time: datetime) -> datetime:
    if time.tzinfo is None or time.utcoffset() is None:
        raise ValueError("a feedback time must be timezone-aware")
    return time.astimezone(UTC)


class FeedbackContext(VaultFileModel):
    """Where the student was when they reported it: enough for the maintainer to reproduce it."""

    subject: str | None = None
    topic: str | None = None
    route: str | None = Field(default=None, max_length=200)
    """The screen: `workspace` (the topic's workspace chat) or `study` (the study chat)."""
    session_id: str | None = None
    mode: FeedbackMode | None = None
    """The topic's mode in the web: Construir or Estudiar."""
    excerpt: str = Field(default="", max_length=MAX_EXCERPT_CHARS)
    """The last lines of the chat before the report, kept short."""


class FeedbackEntry(VaultFileModel):
    """A `record: item` line: one reported bug or improvement."""

    record: Literal["item"] = "item"
    id: str = Field(pattern=FEEDBACK_ID_PATTERN)
    created_at: datetime
    kind: FeedbackKind
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    context: FeedbackContext = Field(default_factory=FeedbackContext)

    @field_validator("created_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        return _aware(value)


class FeedbackStatusChange(VaultFileModel):
    """A `record: status` line: the item's new status and its triage reference."""

    record: Literal["status"] = "status"
    id: str = Field(pattern=FEEDBACK_ID_PATTERN)
    time: datetime
    status: FeedbackStatus
    issue: int | None = Field(default=None, ge=1)

    @field_validator("time")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        return _aware(value)


class FeedbackLine(
    RootModel[Annotated[FeedbackEntry | FeedbackStatusChange, Field(discriminator="record")]]
):
    """One line of the inbox, either kind."""


class FeedbackItem(VaultFileModel):
    """An item as the inbox reads now: its entry plus its latest status."""

    id: str
    created_at: datetime
    kind: FeedbackKind
    title: str
    body: str
    context: FeedbackContext
    status: FeedbackStatus = "nuevo"
    issue: int | None = None
    """The triage reference: the issue of the code repository the item became."""
    updated_at: datetime | None = None


def feedback_path(vault: Vault) -> Path:
    """`feedback/inbox.jsonl` of the vault, whether or not anything was reported yet."""
    return vault.path / FEEDBACK_DIRNAME / INBOX_FILENAME


def _lines(vault: Vault) -> list[FeedbackEntry | FeedbackStatusChange]:
    path = feedback_path(vault)
    if not path.exists():
        return []
    return [line.root for line in read_jsonl(path, FeedbackLine)]


def _fold(lines: list[FeedbackEntry | FeedbackStatusChange]) -> dict[str, FeedbackItem]:
    items: dict[str, FeedbackItem] = {}
    for line in lines:
        if isinstance(line, FeedbackEntry):
            if line.id not in items:  # a repeated id (a merge from another PC) keeps the first
                items[line.id] = FeedbackItem(
                    **line.model_dump(exclude={"record", "context"}), context=line.context
                )
        elif line.id in items:
            items[line.id] = items[line.id].model_copy(
                update={"status": line.status, "issue": line.issue, "updated_at": line.time}
            )
    return items


def _number(feedback_id: str) -> int:
    return int(feedback_id.removeprefix("fb-"))


def list_feedback(vault: Vault, status: FeedbackStatus | None = None) -> list[FeedbackItem]:
    """Every item of the inbox, oldest first, only those in `status` when it is given.

    Raises:
        JsonlError: a complete line of the inbox is not a feedback line.
    """
    items = sorted(_fold(_lines(vault)).values(), key=lambda item: _number(item.id))
    return [item for item in items if status is None or item.status == status]


def get_feedback(vault: Vault, feedback_id: str) -> FeedbackItem:
    """The item `feedback_id`.

    Raises:
        FeedbackNotFoundError: no item has that id.
    """
    item = _fold(_lines(vault)).get(feedback_id)
    if item is None:
        raise FeedbackNotFoundError(f"no feedback item {feedback_id!r}")
    return item


def add_feedback(
    vault: Vault,
    kind: FeedbackKind,
    title: str,
    body: str,
    context: FeedbackContext | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FeedbackItem:
    """Append a new item (status `nuevo`) and return it, creating `feedback/` on first use.

    Raises:
        ValidationError: an empty or too long title/body, an unknown kind.
        VaultBusyError: another process held the inbox lock past its timeout; nothing written.
        SecretRefused: the line looks like it carries a secret; nothing written.
    """
    now = (clock or (lambda: datetime.now(UTC)))()
    path = feedback_path(vault)
    with vault_lock(vault.path, FEEDBACK_LOCK_NAME).hold(FEEDBACK_LOCK_TIMEOUT_SECONDS):
        existing = _fold(_lines(vault))
        number = max((_number(key) for key in existing), default=0) + 1
        entry = FeedbackEntry(
            id=f"fb-{number}",
            created_at=now,
            kind=kind,
            title=" ".join(title.split()),
            body=body.strip(),
            context=context or FeedbackContext(),
        )
        path.parent.mkdir(exist_ok=True)
        append_jsonl(path, entry)
    return FeedbackItem(**entry.model_dump(exclude={"record", "context"}), context=entry.context)


def set_feedback_status(
    vault: Vault,
    feedback_id: str,
    status: FeedbackStatus,
    issue: int | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FeedbackItem:
    """Append a status change of item `feedback_id` and return the item as it reads now.

    `issue` is the triage reference; `None` keeps the one the item already has.

    Raises:
        FeedbackNotFoundError: no item has that id; nothing written.
        VaultBusyError, SecretRefused: as `add_feedback`.
    """
    if not _ID.fullmatch(feedback_id):
        raise FeedbackNotFoundError(f"{feedback_id!r} is not a feedback id (fb-N)")
    now = (clock or (lambda: datetime.now(UTC)))()
    with vault_lock(vault.path, FEEDBACK_LOCK_NAME).hold(FEEDBACK_LOCK_TIMEOUT_SECONDS):
        items = _fold(_lines(vault))
        item = items.get(feedback_id)
        if item is None:
            raise FeedbackNotFoundError(f"no feedback item {feedback_id!r}")
        change = FeedbackStatusChange(
            id=feedback_id,
            time=now,
            status=status,
            issue=issue if issue is not None else item.issue,
        )
        append_jsonl(feedback_path(vault), change)
    return item.model_copy(
        update={"status": change.status, "issue": change.issue, "updated_at": change.time}
    )


__all__ = [
    "FEEDBACK_KINDS",
    "FEEDBACK_STATUSES",
    "FeedbackContext",
    "FeedbackEntry",
    "FeedbackError",
    "FeedbackItem",
    "FeedbackKind",
    "FeedbackMode",
    "FeedbackNotFoundError",
    "FeedbackStatus",
    "FeedbackStatusChange",
    "add_feedback",
    "feedback_path",
    "get_feedback",
    "list_feedback",
    "set_feedback_status",
]
