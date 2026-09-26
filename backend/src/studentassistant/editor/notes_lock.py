"""The short per-topic write lock of `notes/apuntes.md`, shared by the student's saves and the
editor's turns.

A writer holds it only for the read-compare-write-commit step, never across a Claude call: the
student's save (`direct_edit.save_student_edit`) and the application of an editor turn's edit ops
(`revise.revise_notes`) each read the current notes under it, compare their revision with the one
they started from and write only when nothing changed in between. It is a `threading.Lock`, taken
inside the worker thread that does the blocking vault work, so it is not bound to an event loop.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from studentassistant.vault import Vault

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


__all__ = ["WRITE_LOCK_TIMEOUT_SECONDS", "holding_notes", "notes_write_lock"]
