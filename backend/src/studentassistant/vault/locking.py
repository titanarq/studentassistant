"""Cross-process locks on one vault: two processes on one PC never write it at the same time.

The server and a CLI command (`studentassistant import-pdf` while the server runs, or two CLI
commands) are separate processes on one vault. Two things must not overlap between them:
choosing the next source number of a `sources/<kind>/` directory and writing under it, and running
git (two `git add`/`commit` at once fail on git's own `index.lock`, and a `pull --rebase` must not
run under another process's commit). Each is guarded by an advisory `fcntl.flock` on a lock file
under the vault's `.git/` directory -- inside the repository's private directory, so it is never
committed, pushed or listed as vault content.

A `VaultLock` is also the lock of the threads of one process: every thread of the process that
names the same lock file shares one `VaultLock` object, which a thread may take again while it
holds it (only the outermost hold touches the file). Waiting is always bounded: a lock not
obtained within its timeout raises `VaultBusyError`, never hangs.

The locks are the repository's, not one folder's. A vault holds one folder per user
(`users/<user-id>/`, epic #544) and a caller works either through the repository root or through
one of those folders, so `repository_root` turns the path a caller has into the directory holding
`.git/` before anything is named after it: two processes on one vault then share one lock file
whichever handle each of them works through, and a directory's lock is the same lock -- and a real
one, not the in-process fallback of a folder without a `.git/` -- for both.

The operating system drops an `flock` when its process dies, so a crashed process never leaves the
vault locked; the lock files themselves are left in place (they are empty and harmless).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from studentassistant.vault.errors import VaultError
from studentassistant.vault.vault import USERS_DIRNAME

GIT_DIRNAME = ".git"
LOCKS_DIRNAME = "studentassistant-locks"
GIT_LOCK_NAME = "git"
_POLL_SECONDS = 0.02

_REGISTRY: dict[Path, VaultLock] = {}
_REGISTRY_GUARD = threading.Lock()


class VaultBusyError(VaultError):
    """Another process (or thread) kept one of the vault's locks for longer than the timeout."""

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(
            f"la bóveda está ocupada por otro proceso ({name}); no se ha liberado en"
            f" {timeout:g} s -- inténtalo de nuevo en un momento"
        )
        self.name = name
        self.timeout = timeout


class VaultLock:
    """One lock file of a vault, exclusive across processes and threads, re-entrant per thread.

    `path` is `None` for a vault without a `.git/` directory (nowhere private to put the file):
    the lock then only serialises the threads of this process.
    """

    def __init__(self, name: str, path: Path | None) -> None:
        self.name = name
        self.path = path
        self._threads = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    @contextmanager
    def hold(self, timeout: float) -> Iterator[None]:
        """Hold the lock for the `with` body; raise `VaultBusyError` after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        if not self._threads.acquire(timeout=max(timeout, 0.0)):
            raise VaultBusyError(self.name, timeout)
        try:
            if self._depth == 0 and self.path is not None:
                self._fd = _lock_file(self.path, deadline, self.name, timeout)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fd, self._fd = self._fd, None
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
        finally:
            self._threads.release()


def _lock_file(path: Path, deadline: float, name: str, timeout: float) -> int:
    """Open `path` and take an exclusive `flock` on it before `deadline`; return its descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VaultBusyError(name, timeout) from None
                time.sleep(min(_POLL_SECONDS, remaining))
    except BaseException:
        os.close(fd)
        raise


def repository_root(path: Path) -> Path:
    """The vault repository `path` is inside, resolved: where `.git/`, and so every lock, lives.

    `path` is what the caller has, which is the repository root itself (`Vault.open`) or one
    user's folder of it (`Vault.for_user`); anything else -- a vault with no `.git/` yet, a
    directory that is not part of one -- is returned as it is, and gets the in-process lock only.
    """
    resolved = path.resolve()
    if (resolved / GIT_DIRNAME).is_dir():
        return resolved
    users = resolved.parent
    if users.name == USERS_DIRNAME and (users.parent / GIT_DIRNAME).is_dir():
        return users.parent
    return resolved


def vault_lock(vault_root: Path, name: str) -> VaultLock:
    """The lock `name` of the vault at `vault_root`, shared by every thread of the process.

    `vault_root` may be the repository or one of its user folders: the lock is the repository's
    either way, so both name one and the same `VaultLock`.
    """
    root = repository_root(vault_root)
    git_dir = root / GIT_DIRNAME
    path = git_dir / LOCKS_DIRNAME / f"{name}.lock" if git_dir.is_dir() else None
    key = path if path is not None else root / f"<{name}>"
    with _REGISTRY_GUARD:
        lock = _REGISTRY.get(key)
        if lock is None:
            lock = _REGISTRY[key] = VaultLock(name, path)
        return lock


def git_lock(vault_root: Path) -> VaultLock:
    """The lock every git command a vault writer runs is held under."""
    return vault_lock(vault_root, GIT_LOCK_NAME)


def directory_lock(vault_root: Path, directory: Path) -> VaultLock:
    """The lock of one directory of the vault (a `sources/<kind>/`), whether or not it exists.

    Named by the directory's path relative to the repository, so a user handle and the root give
    one and the same directory one and the same lock.
    """
    root = repository_root(vault_root)
    relative = directory.resolve().relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return vault_lock(root, f"dir-{digest}")


__all__ = [
    "GIT_LOCK_NAME",
    "LOCKS_DIRNAME",
    "VaultLock",
    "VaultBusyError",
    "directory_lock",
    "git_lock",
    "repository_root",
    "vault_lock",
]
