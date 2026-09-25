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

The record is metadata about the sync, not content. Its `.gitattributes` line gives it a merge
driver that keeps the remote's side of a rebase, so the record itself can never be a conflict:
whoever claims next overwrites it anyway, and the side kept is the one the check must see.
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
    """`.sa/active.yaml`: which PC claimed the vault, for which session, and whether it let go."""

    host: str
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
    return vault.path / ACTIVE_HOST_NAME


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
) -> ActiveHost:
    """Record `host` as the vault's active writer (session start); returns what was written.

    Also makes sure `.gitattributes` gives the record its merge driver, for vaults created before
    the record existed. Writes files only; committing and pushing is the caller's.
    """
    ensure_active_host_attribute(vault)
    record = ActiveHost(
        host=host,
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

    Only an unreleased claim of another host younger than `stale_after` seconds warns.
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
    message = (
        f"El equipo «{record.host}» tiene abierta {what} desde {since} y puede tener cambios"
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
    """Append the record's merge-driver line to `.gitattributes` if missing; True when it wrote."""
    path = vault.path / GITATTRIBUTES_NAME
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
