"""A stand-in for the sync loop's batch commit racing a writer (#410)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from studentassistant.vault import GitSync


class RacingSyncLoop:
    """Wraps a module's `write_notes`: right after each write, a thread commits what is pending.

    That thread stands for the sync loop's batch commit. It is given half a second to run before
    the writer goes on to its checkpoint; with the git lock held across both it cannot, and
    commits (nothing of the turn's) once the writer is done.
    """

    def __init__(self, sync: GitSync, write: Callable[..., Any]) -> None:
        self.sync, self.write = sync, write
        self.threads: list[threading.Thread] = []
        self.commits: list[str | None] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        written = self.write(*args, **kwargs)
        thread = threading.Thread(target=lambda: self.commits.append(self.sync.checkpoint("lote")))
        thread.start()
        thread.join(0.5)
        self.threads.append(thread)
        return written

    def finish(self) -> None:
        for thread in self.threads:
            thread.join(10)
            assert not thread.is_alive()
