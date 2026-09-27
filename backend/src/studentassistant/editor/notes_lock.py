"""The short per-topic write lock of `notes/apuntes.md`, shared by the student's saves and the
editor's turns.

A writer holds it only for the read-compare-write-commit step, never across a Claude call: the
student's save (`direct_edit.save_student_edit`) and the application of an editor turn's edit ops
(`revise.revise_notes`) each read the current notes under it, compare their revision with the one
they started from and write only when nothing changed in between. It is a `threading.Lock`, taken
inside the worker thread that does the blocking vault work, so it is not bound to an event loop.

`checkpointing(sync)` makes a writer's write and its `GitSync.checkpoint` one step (#410): it holds
the vault's git lock (re-entrant per thread) around both, so the sync loop's batch commit can never
commit the writer's files first and leave its checkpoint -- the commit an undo reverts -- without
them. Order: the notes lock first, then the git lock, never the other way round.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from studentassistant.vault import GitSync, Vault, VaultBusyError

logger = logging.getLogger(__name__)

WRITE_LOCK_TIMEOUT_SECONDS = 60.0
"""How long a writer waits for another one's read-compare-write step before giving up."""

_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_GUARD = threading.Lock()


def notes_write_lock(vault: Vault, subject_slug: str, topic_slug: str) -> threading.Lock:
    """The one write lock of a topic's notes in this process (the same object on every call)."""
    key = (str(vault.path.resolve()), subject_slug, topic_slug)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


@contextmanager
def holding_notes(vault: Vault, subject_slug: str, topic_slug: str) -> Iterator[None]:
    """Hold the topic's write lock (blocking; call from a worker thread).

    Raises:
        TimeoutError: another writer held it for more than `WRITE_LOCK_TIMEOUT_SECONDS`.
    """
    lock = notes_write_lock(vault, subject_slug, topic_slug)
    if not lock.acquire(timeout=WRITE_LOCK_TIMEOUT_SECONDS):
        raise TimeoutError(f"the notes of {subject_slug}/{topic_slug} stayed locked")
    try:
        yield
    finally:
        lock.release()


Checkpoint = Callable[[str], str | None]
"""`commit(message)`: note the change and commit every pending file now; the commit or `None`."""


@contextmanager
def checkpointing(sync: GitSync) -> Iterator[Checkpoint]:
    """Hold the vault's git lock for a write and the checkpoint that follows it (blocking).

    Yields `commit(message)`, to call after the writes inside the `with` body; it returns the new
    commit, or `None` (nothing to commit, or a failure kept in `sync.status().last_error`). When
    the git lock stays busy past `[vault.git] timeout_seconds` the body still runs, unlocked, and
    `commit` returns `None` without waiting a second time: the files are committed later by the
    sync loop, and that change cannot be undone.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(sync.locked())
        except VaultBusyError as busy:
            logger.warning("vault git lock busy; this change is committed later: %s", busy)

            def commit(message: str) -> str | None:
                sync.note_change()
                return None

        else:

            def commit(message: str) -> str | None:
                sync.note_change()
                return sync.checkpoint(message)

        yield commit


__all__ = [
    "WRITE_LOCK_TIMEOUT_SECONDS",
    "Checkpoint",
    "checkpointing",
    "holding_notes",
    "notes_write_lock",
]
