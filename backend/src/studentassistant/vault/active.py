"""The active-host record: which PC is writing the vault right now (ADR-0002, one active writer).

One student may use the vault from several PCs, but only one of them should be capturing at a
time: two PCs writing the same topic diverge, and what `merge=union` cannot merge ends up in front
of the student. So a session start records its host in `.sa/active.yaml` (`claim_active_host`)
and a session end marks it released (`release_active_host`); both are committed and pushed by
whoever wires them to a `GitSync` (`server`).

Before claiming, the session start pulls and reads the record another PC may have pushed
(`check_active_host`): a record of another host that was never released and is younger than the
configured staleness means that PC has a session open, and quite possibly activity it has not
pushed yet. That is a warning (`ActiveHostWarning`, Spanish, shown to the student), never a refusal:
the PC may simply have been switched off mid-session, which is also what staleness is for.

The record is metadata about the sync, not content, and it is the repository's rather than one
student's: one PC captures at a time whatever user it captures as, so there is one record for the
whole vault, at `vault.root`, and every function here reads and writes it there whichever handle it
is given -- a user's folder holds no `.sa/` of its own. What the handle does say is who is
capturing: a claim made through a user's handle records that user (`user`), which is what lets the
warning name them and the session start tell one student that another is already capturing.

Its `.gitattributes` line gives the record a merge driver that keeps the remote's side of a rebase,
so the record itself can never be a conflict: whoever claims next overwrites it anyway, and the
side kept is the one the check must see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.files import read_yaml, write_text_atomic, write_yaml_atomic
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.vault import (
    ACTIVE_HOST_GITATTRIBUTES_LINE,
    ACTIVE_HOST_NAME,
    GITATTRIBUTES_NAME,
    Vault,
)

logger = logging.getLogger(__name__)


class ActiveHost(VaultFileModel):
    """`.sa/active.yaml`: which PC claimed the vault, as whom, for which session, and whether it
    let go.

    `user` is the id of the student capturing, and is `None` in a record written before a vault
    held several users (epic #544): such a record still reads, and simply does not say who it was.
    """

    host: str
    user: str | None = None
    session_id: str | None = None
    subject: str | None = None
    topic: str | None = None
    claimed_at: datetime
    # Set by the session end; a record without it is a session still open on `host`.
    released_at: datetime | None = None

    @property
    def released(self) -> bool:
        return self.released_at is not None


@dataclass(frozen=True)
class ActiveHostWarning:
    """Another PC holds the vault: its record, and the Spanish message the student is shown."""

    record: ActiveHost
    message: str


def active_host_path(vault: Vault) -> Path:
    """The record's path: the repository's, whichever handle `vault` is."""
    return vault.root / ACTIVE_HOST_NAME


def read_active_host(vault: Vault) -> ActiveHost | None:
    """The record as it is in the working tree, or `None` when there is none or it is unreadable.

    An unreadable record is logged and treated as absent: it only feeds a warning, and the next
    claim rewrites it.
    """
    path = active_host_path(vault)
    try:
        return read_yaml(path, ActiveHost)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, YAMLError, ValidationError) as error:
        logger.warning("ignoring an unreadable %s: %s", ACTIVE_HOST_NAME, error)
        return None


def claim_active_host(
    vault: Vault,
    host: str,
    session_id: str | None = None,
    subject_slug: str | None = None,
    topic_slug: str | None = None,
    claimed_at: datetime | None = None,
    *,
    user_id: str | None = None,
) -> ActiveHost:
    """Record `host` as the vault's active writer (session start); returns what was written.

    The user capturing is `user_id`, or the one whose handle `vault` is when it is a user's, so a
    session start that already works through a user's handle records who it is for without being
    told; a claim on the root handle records no user unless one is given.

    Also makes sure `.gitattributes` gives the record its merge driver, for vaults created before
    the record existed. Writes files only; committing and pushing is the caller's.
    """
    ensure_active_host_attribute(vault)
    record = ActiveHost(
        host=host,
        user=user_id if user_id is not None else vault.user_id,
        session_id=session_id,
        subject=subject_slug,
        topic=topic_slug,
        claimed_at=(claimed_at or datetime.now(UTC)).astimezone(UTC),
    )
    path = active_host_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml_atomic(path, record)
    return record


def release_active_host(
    vault: Vault,
    host: str,
    session_id: str | None = None,
    released_at: datetime | None = None,
) -> ActiveHost | None:
    """Mark the record released (session end) when it is still `host`'s claim for `session_id`.

    Returns the record written, or `None` when there is nothing to release: no record, one
    already released, or one another PC (or another session) has claimed since -- a newer claim
    is never overwritten.
    """
    record = read_active_host(vault)
    if record is None or record.released or record.host != host:
        return None
    if session_id is not None and record.session_id != session_id:
        return None
    released = record.model_copy(
        update={"released_at": (released_at or datetime.now(UTC)).astimezone(UTC)}
    )
    write_yaml_atomic(active_host_path(vault), released)
    return released


def is_stale(record: ActiveHost, now: datetime, stale_after: float) -> bool:
    """Whether a claim is older than `stale_after` seconds at `now`."""
    return now - record.claimed_at > timedelta(seconds=stale_after)


def active_host_warning(
    record: ActiveHost | None, host: str, stale_after: float, now: datetime | None = None
) -> ActiveHostWarning | None:
    """The warning `record` deserves as seen from `host`, or `None`.

    Only an unreleased claim of another host younger than `stale_after` seconds warns. The message
    names the student that host is capturing as when the record says who it is, because on a vault
    several students share, "another PC is capturing" is only half of what the student needs to
    know to decide whether to wait.
    """
    if record is None or record.released or record.host == host:
        return None
    now = now or datetime.now(UTC)
    if is_stale(record, now, stale_after):
        logger.info(
            "ignoring the stale active-host claim of %s (since %s)", record.host, record.claimed_at
        )
        return None
    since = record.claimed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    what = f"la sesión {record.session_id}" if record.session_id else "una sesión"
    if record.subject and record.topic:
        what += f" ({record.subject}/{record.topic})"
    who = f"El equipo «{record.host}»"
    if record.user:
        who += f", con el usuario «{record.user}»,"
    message = (
        f"{who} tiene abierta {what} desde {since} y puede tener cambios"
        " sin subir. Termínala allí y deja que se sincronice antes de seguir aquí, o los dos"
        " equipos divergirán."
    )
    return ActiveHostWarning(record=record, message=message)


def check_active_host(
    vault: Vault, host: str, stale_after: float, now: datetime | None = None
) -> ActiveHostWarning | None:
    """Read the record (pull first) and say whether another PC holds the vault."""
    return active_host_warning(read_active_host(vault), host, stale_after, now)


def ensure_active_host_attribute(vault: Vault) -> bool:
    """Append the record's merge-driver line to `.gitattributes` if missing; True when it wrote.

    `.gitattributes` is the repository's, at `vault.root`, like the record it drives.
    """
    path = vault.root / GITATTRIBUTES_NAME
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    if ACTIVE_HOST_GITATTRIBUTES_LINE in content.splitlines(keepends=True) or (
        content.endswith(ACTIVE_HOST_GITATTRIBUTES_LINE.rstrip("\n"))
    ):
        return False
    if content and not content.endswith("\n"):
        content += "\n"
    write_text_atomic(path, content + ACTIVE_HOST_GITATTRIBUTES_LINE)
    return True


__all__ = [
    "ActiveHost",
    "ActiveHostWarning",
    "active_host_path",
    "active_host_warning",
    "check_active_host",
    "claim_active_host",
    "ensure_active_host_attribute",
    "is_stale",
    "read_active_host",
    "release_active_host",
]
