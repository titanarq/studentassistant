"""Vault git sync: batched commits, checkpoints, debounced push with backoff, pull and tags.

ADR-0002 in code. Writers (`sessions.py`, `sources.py`, ...) only write files; whoever wires them
to a `GitSync` calls `note_change()` after each write, which is cheap and never runs git. The
pending changes are then committed once nothing changed for `commit_quiet_seconds` (or at the latest
`commit_max_delay_seconds` after the first one, so a steady stream of segments still gets
committed), with a Spanish message that sums up what the batch holds
(`sesión 20260924-183000: 12 segmentos, 2 capturas`). `checkpoint(message)` commits at once.

A new commit is pushed `push_debounce_seconds` after the first unpushed one, and at once by
`flush()` (session end, shutdown). A failed push -- offline, refused credentials, rejected by a
remote that moved on -- is kept in the `SyncStatus` and retried with exponential backoff; it is
never raised, so the capture path never sees it, and a local commit is never undone by it.

`sync()` is `git pull --rebase`: the JSONL logs merge by union (`.gitattributes`), and any other
conflict aborts the rebase, so the local commits and the working tree stay exactly as they were
and the conflicting paths are reported for the student to decide (ADR-0002: never auto-resolved
by discarding). Notes versions are annotated tags `<topic-slug>/apuntes-vN`, pushed with the branch.

Time is read from an injectable `Clock`, and nothing here sleeps or starts a thread: `run_due()`
does whatever is due now, and `run()` is the asyncio loop that calls it in a worker thread, so git
never blocks the event loop. Every git command is serialised by one lock.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from studentassistant.config import VaultGitSettings
from studentassistant.vault.git import GitIdentity, GitResult, GitRunner
from studentassistant.vault.vault import MAIN_BRANCH, Vault

logger = logging.getLogger(__name__)

FailureKind = Literal["offline", "auth", "rejected", "error"]
SyncOutcome = Literal["ok", "conflict", "offline", "auth", "error"]

NOTES_TAG_SUFFIX = "apuntes-v"
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Clock(Protocol):
    """Where the sync reads the time from; seconds, only differences matter."""

    def monotonic(self) -> float: ...


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class PushFailure:
    """Why the last push failed; `message` is redacted and safe to show."""

    kind: FailureKind
    message: str
    at: datetime


@dataclass(frozen=True)
class SyncResult:
    """What one `sync()` did. `conflicts` lists the paths a rebase could not merge."""

    outcome: SyncOutcome
    message: str = ""
    conflicts: tuple[str, ...] = ()
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@dataclass(frozen=True)
class SyncStatus:
    """A snapshot of the vault's git state, for the web UI and the logs. Reading it runs no git."""

    pending_changes: bool = False
    last_commit: str | None = None
    last_commit_at: datetime | None = None
    # Local commits the remote does not have yet (as of the last commit, push or sync).
    pending_commits: int = 0
    last_push_at: datetime | None = None
    last_push_failure: PushFailure | None = None
    consecutive_push_failures: int = 0
    # When the next push is due, on the sync's clock; `None` when nothing is scheduled.
    next_push_due: float | None = None
    last_sync: SyncResult | None = None
    # The last commit or tag that failed, redacted.
    last_error: str | None = None


@dataclass(frozen=True)
class NotesTag:
    """One notes version: `<topic-slug>/apuntes-v<version>` on `commit`."""

    name: str
    version: int
    commit: str


def notes_tag_name(topic_slug: str, version: int) -> str:
    return f"{topic_slug}/{NOTES_TAG_SUFFIX}{version}"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


_COUNT_WORDS = {
    "segmentos": ("segmento", "segmentos"),
    "eventos": ("evento", "eventos"),
    "capturas": ("captura", "capturas"),
    "fuentes": ("fuente", "fuentes"),
}


def _counts_text(counts: Counter[str]) -> str:
    return ", ".join(
        _plural(counts[key], *_COUNT_WORDS[key]) for key in _COUNT_WORDS if counts[key]
    )


def summarize_changes(added_lines: dict[str, int], new_files: set[str]) -> str:
    """The Spanish summary of a batch, from the lines each staged path adds and the new paths.

    Transcript lines count as segments and event lines as events, per session; a new source
    sidecar counts as a capture (`sources/notes/`) or a source (any other kind). Paths none of that
    covers are only counted as changed files, when nothing else describes the batch.
    """
    per_session: dict[str, Counter[str]] = defaultdict(Counter)
    sources: Counter[str] = Counter()
    for path, added in added_lines.items():
        parts = path.split("/")
        if "sessions" in parts:
            index = parts.index("sessions")
            if len(parts) == index + 3:
                session_id, name = parts[index + 1], parts[index + 2]
                if name == "transcript.jsonl":
                    per_session[session_id]["segmentos"] += added
                elif name == "events.jsonl":
                    per_session[session_id]["eventos"] += added
                else:
                    per_session.setdefault(session_id, Counter())
        if "sources" in parts and path in new_files and path.endswith(".yaml"):
            index = parts.index("sources")
            if len(parts) == index + 3:
                sources["capturas" if parts[index + 1] == "notes" else "fuentes"] += 1

    sessions = sorted(per_session)
    if len(sessions) == 1:
        per_session[sessions[0]].update(sources)
        sources = Counter()
    parts_text = []
    for session_id in sessions:
        counts = _counts_text(per_session[session_id])
        parts_text.append(f"sesión {session_id}" + (f": {counts}" if counts else ""))
    if sources:
        parts_text.append(_counts_text(sources))
    if parts_text:
        return "; ".join(parts_text)
    return _plural(len(added_lines), "archivo cambiado", "archivos cambiados")


def _classify(result: GitResult) -> FailureKind:
    """Tell an offline remote from refused credentials from a rejected push."""
    if result.timed_out:
        return "offline"
    text = f"{result.stderr}\n{result.stdout}"
    if re.search(r"\[rejected\]|non-fast-forward|fetch first|\[remote rejected\]", text):
        return "rejected"
    if re.search(
        r"Authentication failed|Permission denied|could not read Username|could not read Password"
        r"|terminal prompts disabled|returned error: 40[13]|Invalid username or password",
        text,
        re.IGNORECASE,
    ):
        return "auth"
    if re.search(
        r"Could not resolve host|unable to access|Connection refused|Connection timed out"
        r"|Network is unreachable|does not appear to be a git repository"
        r"|Could not read from remote repository|Operation timed out|No route to host",
        text,
        re.IGNORECASE,
    ):
        return "offline"
    return "error"


class GitSync:
    """Commits, pushes and pulls one vault. Thread-safe; one per vault per process."""

    def __init__(
        self,
        vault: Vault,
        settings: VaultGitSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.vault = vault
        self.settings = settings or VaultGitSettings()
        self.clock: Clock = clock or SystemClock()
        self.identity = GitIdentity(
            name=self.settings.author_name or vault.meta.student,
            email=self.settings.author_email,
        )
        self.git = GitRunner(vault.path, self.identity, timeout=self.settings.timeout_seconds)
        # `_git_lock` serialises git; `_state_lock` guards the fields below and is never held
        # while git runs, so `note_change()` and `status()` never wait for a push.
        self._git_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._first_change_at: float | None = None
        self._last_change_at: float | None = None
        self._push_due_at: float | None = None
        self._status = SyncStatus()

    # -- state ---------------------------------------------------------------------------------

    def status(self) -> SyncStatus:
        with self._state_lock:
            return replace(
                self._status,
                pending_changes=self._last_change_at is not None,
                next_push_due=self._push_due_at,
            )

    def _update(self, **changes: object) -> None:
        with self._state_lock:
            self._status = replace(self._status, **changes)  # type: ignore[arg-type]

    def note_change(self) -> None:
        """Record that a vault file was written; the batch commits after the quiet period."""
        now = self.clock.monotonic()
        with self._state_lock:
            if self._first_change_at is None:
                self._first_change_at = now
            self._last_change_at = now

    def _schedule_push(self, delay: float) -> None:
        with self._state_lock:
            if self._push_due_at is None:
                self._push_due_at = self.clock.monotonic() + delay

    # -- commits -------------------------------------------------------------------------------

    def checkpoint(self, message: str) -> str | None:
        """Commit every pending change now under `message`; the batch summary goes in the body.

        Returns the new commit, or `None` when there was nothing to commit or the commit failed
        (the failure is in `status().last_error`; it is never raised).
        """
        with self._git_lock:
            return self._commit(message)

    def run_due(self) -> None:
        """Commit the pending batch and push if their time has come. Never raises."""
        now = self.clock.monotonic()
        with self._state_lock:
            commit_due = self._last_change_at is not None and (
                now - self._last_change_at >= self.settings.commit_quiet_seconds
                or now - (self._first_change_at or now) >= self.settings.commit_max_delay_seconds
            )
            push_due = self._push_due_at is not None and now >= self._push_due_at
        if commit_due:
            with self._git_lock:
                self._commit(None)
        if push_due:
            self.push_now()

    def _commit(self, subject: str | None) -> str | None:
        """Stage everything and commit it; `subject=None` makes the summary the subject.

        The caller holds `_git_lock`. Changes noted while this runs stay pending for the next batch.
        """
        with self._state_lock:
            first, last = self._first_change_at, self._last_change_at
            self._first_change_at = self._last_change_at = None
        try:
            sha = self._commit_locked(subject)
        except _CommitError as failure:
            with self._state_lock:
                # Put the batch back, unless newer changes already reopened one.
                if self._last_change_at is None:
                    self._first_change_at, self._last_change_at = first, last
            logger.warning("vault commit failed: %s", failure.message)
            self._update(last_error=failure.message)
            return None
        if sha is not None:
            self._update(
                last_commit=sha,
                last_commit_at=datetime.now(UTC),
                pending_commits=self._count_unpushed(),
                last_error=None,
            )
            self._schedule_push(self.settings.push_debounce_seconds)
        return sha

    def _commit_locked(self, subject: str | None) -> str | None:
        self._must(self.git.run("add", "--all"))
        if self.git.run("diff", "--cached", "--quiet").returncode == 0:
            return None  # no empty commits
        summary = self._staged_summary()
        arguments = ["-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify"]
        if subject is None:
            arguments += ["-m", summary]
        else:
            arguments += ["-m", subject, "-m", summary]
        self._must(self.git.run(*arguments))
        return self._must(self.git.run("rev-parse", "HEAD")).stdout.strip()

    def _staged_summary(self) -> str:
        numstat = self._must(self.git.run("diff", "--cached", "--numstat", "--no-renames", "-z"))
        added: dict[str, int] = {}
        for entry in numstat.stdout.split("\0"):
            if not entry:
                continue
            added_text, _deleted, path = entry.split("\t", 2)
            added[path] = int(added_text) if added_text.isdigit() else 0
        new = self._must(
            self.git.run("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=A", "-z")
        )
        return summarize_changes(added, {path for path in new.stdout.split("\0") if path})

    @staticmethod
    def _must(result: GitResult) -> GitResult:
        if not result.ok:
            raise _CommitError(result.describe())
        return result

    def _count_unpushed(self) -> int:
        result = self.git.run(
            "rev-list", "--count", "HEAD", "--not", f"--remotes={self.settings.remote}"
        )
        return int(result.stdout.strip()) if result.ok and result.stdout.strip() else 0

    # -- push ----------------------------------------------------------------------------------

    def push_now(self) -> bool:
        """Push the branch and its notes tags now; on failure schedule a retry. Never raises."""
        with self._git_lock:
            result = self.git.run(
                "push", "--follow-tags", "--porcelain", self.settings.remote, MAIN_BRANCH
            )
            pending = self._count_unpushed()
        now = self.clock.monotonic()
        if result.ok:
            with self._state_lock:
                self._push_due_at = None
                self._status = replace(
                    self._status,
                    last_push_at=datetime.now(UTC),
                    last_push_failure=None,
                    consecutive_push_failures=0,
                    pending_commits=pending,
                )
            return True
        kind = _classify(result)
        with self._state_lock:
            failures = self._status.consecutive_push_failures + 1
            delay = min(
                self.settings.push_backoff_initial_seconds * 2 ** (failures - 1),
                self.settings.push_backoff_max_seconds,
            )
            self._push_due_at = now + delay
            self._status = replace(
                self._status,
                last_push_failure=PushFailure(
                    kind=kind, message=result.describe(), at=datetime.now(UTC)
                ),
                consecutive_push_failures=failures,
                pending_commits=pending,
            )
        logger.warning("vault push failed (%s), retrying in %.0f s", kind, delay)
        return False

    def flush(self) -> SyncStatus:
        """Commit whatever is pending and push now: session end and shutdown. Never raises."""
        with self._git_lock:
            self._commit(None)
        self.push_now()
        return self.status()

    # -- pull ----------------------------------------------------------------------------------

    def sync(self) -> SyncResult:
        """Commit what is pending, then `git pull --rebase` from the remote. Never raises.

        JSONL files merge by union. Any other conflict aborts the rebase: HEAD, the local commits
        and the working tree are left as they were before, and the result lists the paths.
        """
        with self._git_lock:
            self._commit(None)
            result = self._pull_locked()
            pending = self._count_unpushed()
        self._update(last_sync=result, pending_commits=pending)
        if result.ok and pending:
            self._schedule_push(0.0)
        if not result.ok:
            logger.warning("vault sync: %s %s", result.outcome, result.message)
        return result

    def _pull_locked(self) -> SyncResult:
        remote = self.settings.remote
        heads = self.git.run("ls-remote", "--heads", remote, MAIN_BRANCH)
        if not heads.ok:
            return SyncResult(outcome=_sync_kind(heads), message=heads.describe())
        if not heads.stdout.strip():
            return SyncResult(outcome="ok", message="el remoto aún no tiene la rama principal")
        pull = self.git.run(
            "-c", "commit.gpgsign=false", "pull", "--rebase", "--no-autostash", remote, MAIN_BRANCH
        )
        if pull.ok:
            return SyncResult(outcome="ok")
        if self._rebase_in_progress():
            unmerged = self.git.run("diff", "--name-only", "--diff-filter=U", "-z")
            conflicts = tuple(sorted(path for path in unmerged.stdout.split("\0") if path))
            abort = self.git.run("rebase", "--abort")
            message = "conflicto no resoluble automáticamente; rebase cancelado"
            if not abort.ok:
                message += f" (y la cancelación falló: {abort.describe()})"
            return SyncResult(outcome="conflict", message=message, conflicts=conflicts)
        return SyncResult(outcome=_sync_kind(pull), message=pull.describe())

    def _rebase_in_progress(self) -> bool:
        for name in ("rebase-merge", "rebase-apply"):
            result = self.git.run("rev-parse", "--git-path", name)
            if result.ok and (self.vault.path / result.stdout.strip()).exists():
                return True
        return False

    # -- notes version tags --------------------------------------------------------------------

    def list_notes_tags(self, topic_slug: str) -> list[NotesTag]:
        """Every `<topic-slug>/apuntes-vN` tag, oldest version first."""
        _check_slug(topic_slug)
        with self._git_lock:
            return self._list_tags_locked(topic_slug)

    def _list_tags_locked(self, topic_slug: str) -> list[NotesTag]:
        result = self.git.run(
            "tag",
            "--list",
            f"{topic_slug}/{NOTES_TAG_SUFFIX}*",
            "--format=%(refname:short)%09%(*objectname)%09%(objectname)",
        )
        pattern = re.compile(rf"^{re.escape(topic_slug)}/{NOTES_TAG_SUFFIX}([1-9][0-9]*)$")
        tags = []
        for line in result.stdout.splitlines() if result.ok else []:
            name, peeled, target = line.split("\t")
            match = pattern.match(name)
            if match:
                tags.append(NotesTag(name=name, version=int(match[1]), commit=peeled or target))
        return sorted(tags, key=lambda tag: tag.version)

    def create_notes_tag(self, topic_slug: str, message: str | None = None) -> NotesTag:
        """Commit what is pending and tag HEAD as the topic's next notes version.

        The tag is annotated, so the next push carries it along with the branch.

        Raises:
            ValueError: when `topic_slug` is not a slug.
            GitCommandError: when git refuses the tag (this is not the capture path).
        """
        _check_slug(topic_slug)
        with self._git_lock:
            self._commit(None)
            existing = self._list_tags_locked(topic_slug)
            version = (existing[-1].version if existing else 0) + 1
            name = notes_tag_name(topic_slug, version)
            self.git.check(
                "tag", "--annotate", name, "--message", message or f"apuntes v{version}", "HEAD"
            )
            commit = self.git.check("rev-parse", "HEAD").stdout.strip()
        self._schedule_push(self.settings.push_debounce_seconds)
        return NotesTag(name=name, version=version, commit=commit)

    # -- scheduling ----------------------------------------------------------------------------

    async def run(self, interval: float = 1.0) -> None:
        """Call `run_due()` every `interval` seconds in a worker thread, until cancelled."""
        while True:
            await asyncio.to_thread(self.run_due)
            await asyncio.sleep(interval)


class _CommitError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _sync_kind(result: GitResult) -> SyncOutcome:
    kind = _classify(result)
    return "error" if kind == "rejected" else kind


def _check_slug(topic_slug: str) -> None:
    if not _SLUG.match(topic_slug):
        raise ValueError(f"{topic_slug!r} is not a topic slug")


__all__ = [
    "Clock",
    "GitSync",
    "NotesTag",
    "PushFailure",
    "SyncResult",
    "SyncStatus",
    "SystemClock",
    "notes_tag_name",
    "summarize_changes",
]
