"""Helpers the git sync tests share: a manual clock, a second clone, and git as a test sees it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from studentassistant.vault import Vault


class ManualClock:
    """Seconds that only move when a test says so, so no test waits for a real quiet period."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` for a test's own inspection or setup, and return its stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout


def commit_count(cwd: Path, ref: str = "HEAD") -> int:
    return int(git(cwd, "rev-list", "--count", ref).strip())


def clone_vault(origin: Path, path: Path) -> Vault:
    """A second PC: the vault cloned from `origin` into `path` and opened."""
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(path)], check=True, capture_output=True
    )
    return Vault.open(path)
