"""The feedback inbox: bugs and improvements of the app itself the student reports in a chat (#472).

A chat turn that recognises app feedback («apunta una mejora: ...», «esto es un bug: ...») records
one item here; the maintainer reads the inbox with `studentassistant feedback list` and triages each
item into an issue of the code repository by hand -- the backend never calls GitHub (the code
repository is public, the vault is the student's private content).

The inbox is `feedback/inbox.jsonl` at the vault root, append-only JSONL like every other log
(`append_jsonl`: secret guard, fsynced; `*.jsonl` merges with `union`). It is about the app, not
about one student's content, so there is one for the whole repository and every function here reads
and writes it at `vault.root` whichever handle it is given; what a user's handle adds is who
reported the item, recorded in its context (`user`) so the maintainer can ask that student and so a
report of one student is not read as another's. Two kinds of line:

- `{"record": "item", ...}` -- a new item: `id` (below), `created_at`, `kind` (`bug` |
  `mejora`), `title`, `body`, `context`;
- `{"record": "status", "id", "time", "status", "issue"}` -- a later status change (`nuevo`,
  `triado`, `descartado`) with its triage reference (an issue number, or `None`).

An id is `fb-` plus six random characters of a lower-case alphabet without look-alikes
(`fb-k7m2qx`, #476): two PCs that both report something before their vaults sync give their items
different ids without talking to each other, and it stays short enough to type in
`studentassistant feedback mark`. Inboxes written before #476 hold `fb-N` ids (one past the
highest in the file); they are still read, folded and marked, but two PCs may have allocated the
same `fb-N` before a union merge. Such an id is ambiguous: every item line is kept and listed, a
status change with that id applies to all of them (nothing tells which PC's item it meant), and
`set_feedback_status` refuses it with `FeedbackAmbiguousError` naming every item.

Reading folds the lines in file order: an item's status is its last change (`nuevo` without one).
Every write -- allocating the id and appending, or checking the id and appending a change -- runs
under the cross-process vault lock `feedback` (`locking.py`), taken on the repository so that a
user's handle and the root hold one and the same lock and the server and the CLI of one PC never
give two items one id. Nothing here runs git: the caller commits (the sync loop, or a checkpoint).
"""

from __future__ import annotations

import re
import secrets
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
FEEDBACK_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
"""The characters of a new id's random part: lower case, no `0`/`o`, `1`/`l`/`i` look-alikes."""
FEEDBACK_ID_RANDOM_CHARS = 6
FEEDBACK_ID_PATTERN = (
    rf"^fb-(?:[1-9][0-9]*|[{FEEDBACK_ID_ALPHABET}]{{{FEEDBACK_ID_RANDOM_CHARS}}})$"
)
"""A new id (`fb-` + six random characters) or a legacy one (`fb-N`, before #476)."""
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


class FeedbackAmbiguousError(FeedbackError):
    """More than one item of the inbox has that id (a legacy `fb-N` two PCs both allocated)."""

    def __init__(self, feedback_id: str, items: list[FeedbackItem]) -> None:
        self.feedback_id = feedback_id
        self.items = items
        titles = ", ".join(repr(item.title) for item in items)
        super().__init__(f"{len(items)} feedback items share the id {feedback_id!r}: {titles}")


def collapse_title(title: str) -> str:
    """`title` as the inbox stores it: runs of whitespace collapsed to one space, trimmed.

    `MAX_TITLE_CHARS` counts the collapsed title.
    """
    return " ".join(title.split())


def _aware(time: datetime) -> datetime:
    if time.tzinfo is None or time.utcoffset() is None:
        raise ValueError("a feedback time must be timezone-aware")
    return time.astimezone(UTC)


class FeedbackContext(VaultFileModel):
    """Where the student was when they reported it: enough for the maintainer to reproduce it."""

    user: str | None = None
    """The id of the student who reported it; `None` in a line written before a vault had users."""
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

    @field_validator("title", mode="before")
    @classmethod
    def one_line(cls, value: object) -> object:
        return collapse_title(value) if isinstance(value, str) else value

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
    """`feedback/inbox.jsonl` of the vault, whether or not anything was reported yet.

    The repository's inbox, at `vault.root`: a user's handle has no inbox of its own, so every
    student's reports land in the one file the maintainer reads.
    """
    return vault.root / FEEDBACK_DIRNAME / INBOX_FILENAME


def _lines(vault: Vault) -> list[FeedbackEntry | FeedbackStatusChange]:
    path = feedback_path(vault)
    if not path.exists():
        return []
    return [line.root for line in read_jsonl(path, FeedbackLine)]


def _item(entry: FeedbackEntry) -> FeedbackItem:
    return FeedbackItem(**entry.model_dump(exclude={"record", "context"}), context=entry.context)


def _fold(lines: list[FeedbackEntry | FeedbackStatusChange]) -> dict[str, list[FeedbackItem]]:
    """Every item line by id, in file order; a status change applies to every item of its id.

    More than one item under an id is a legacy `fb-N` two PCs both allocated before a union merge:
    all are kept, and a change with that id cannot say which it meant, so it applies to each.
    A change for an id with no item (yet) is ignored.
    """
    items: dict[str, list[FeedbackItem]] = {}
    for line in lines:
        if isinstance(line, FeedbackEntry):
            items.setdefault(line.id, []).append(_item(line))
        elif line.id in items:
            update = {"status": line.status, "issue": line.issue, "updated_at": line.time}
            items[line.id] = [item.model_copy(update=update) for item in items[line.id]]
    return items


def _new_id(taken: set[str]) -> str:
    while True:
        suffix = "".join(
            secrets.choice(FEEDBACK_ID_ALPHABET) for _ in range(FEEDBACK_ID_RANDOM_CHARS)
        )
        # an all-digit suffix would read as a legacy `fb-N`; such a one is simply drawn again
        if not suffix.isdigit() and f"fb-{suffix}" not in taken:
            return f"fb-{suffix}"


def _normalized(feedback_id: str) -> str:
    return feedback_id.strip().lower()


def _with_user(context: FeedbackContext, user_id: str | None) -> FeedbackContext:
    """`context` naming the user the handle is one for, when it does not already name one."""
    if user_id is None or context.user is not None:
        return context
    return context.model_copy(update={"user": user_id})


def list_feedback(vault: Vault, status: FeedbackStatus | None = None) -> list[FeedbackItem]:
    """Every item of the inbox, oldest first, only those in `status` when it is given.

    Oldest is by `created_at`, file order among equal times. Items sharing a legacy id are all
    listed (with the same id).

    Raises:
        JsonlError: a complete line of the inbox is not a feedback line.
    """
    folded = [item for group in _fold(_lines(vault)).values() for item in group]
    items = sorted(folded, key=lambda item: item.created_at)
    return [item for item in items if status is None or item.status == status]


def get_feedback(vault: Vault, feedback_id: str) -> FeedbackItem:
    """The item `feedback_id` (case and surrounding spaces ignored).

    Raises:
        FeedbackNotFoundError: no item has that id.
        FeedbackAmbiguousError: more than one item has it (a legacy duplicate).
    """
    return _single(_fold(_lines(vault)), _normalized(feedback_id))


def _single(items: dict[str, list[FeedbackItem]], feedback_id: str) -> FeedbackItem:
    group = items.get(feedback_id)
    if not group:
        raise FeedbackNotFoundError(f"no feedback item {feedback_id!r}")
    if len(group) > 1:
        raise FeedbackAmbiguousError(feedback_id, group)
    return group[0]


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

    An item reported through a user's handle records that user in its context, unless the caller's
    context already names one: who reported it is what the maintainer needs to ask about it, and
    the caller that has the handle is the one that knows.

    Raises:
        ValidationError: an empty or too long title/body, an unknown kind.
        VaultBusyError: another process held the inbox lock past its timeout; nothing written.
        SecretRefused: the line looks like it carries a secret; nothing written.
    """
    now = (clock or (lambda: datetime.now(UTC)))()
    path = feedback_path(vault)
    with vault_lock(vault.root, FEEDBACK_LOCK_NAME).hold(FEEDBACK_LOCK_TIMEOUT_SECONDS):
        entry = FeedbackEntry(
            id=_new_id(set(_fold(_lines(vault)))),
            created_at=now,
            kind=kind,
            title=title,
            body=body.strip(),
            context=_with_user(context or FeedbackContext(), vault.user_id),
        )
        path.parent.mkdir(exist_ok=True)
        append_jsonl(path, entry)
    return _item(entry)


def set_feedback_status(
    vault: Vault,
    feedback_id: str,
    status: FeedbackStatus,
    issue: int | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FeedbackItem:
    """Append a status change of item `feedback_id` and return the item as it reads now.

    `issue` is the triage reference; `None` keeps the one the item already has. The id's case and
    surrounding spaces are ignored.

    Raises:
        FeedbackNotFoundError: no item has that id; nothing written.
        FeedbackAmbiguousError: more than one item has it (a legacy `fb-N` two PCs both
            allocated); nothing written -- a change could not say which item it meant.
        VaultBusyError, SecretRefused: as `add_feedback`.
    """
    feedback_id = _normalized(feedback_id)
    if not _ID.fullmatch(feedback_id):
        raise FeedbackNotFoundError(f"{feedback_id!r} is not a feedback id")
    now = (clock or (lambda: datetime.now(UTC)))()
    with vault_lock(vault.root, FEEDBACK_LOCK_NAME).hold(FEEDBACK_LOCK_TIMEOUT_SECONDS):
        item = _single(_fold(_lines(vault)), feedback_id)
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
    "MAX_TITLE_CHARS",
    "FeedbackAmbiguousError",
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
    "collapse_title",
    "feedback_path",
    "get_feedback",
    "list_feedback",
    "set_feedback_status",
]
