"""The git runner: every git command the vault runs goes through here (ADR-0002).

Git is run as a subprocess inside the vault root, never through a library, and always with the
configured student identity as author and committer, passed in the environment so neither the
repository's nor the user's git configuration decides whose name a vault commit carries. A command
never prompts (a prompt would hang a background push forever), and is killed after a timeout.

A failed command is a `GitResult` whose `ok` is false, not an exception: the sync layer decides
what a failure means. Whatever git printed is passed through `redact` before it is kept, because a
remote URL can carry a token (`https://user:token@github.com/...`) and git echoes it in errors that
end up in logs and in the status the web UI shows.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from studentassistant.vault.errors import VaultError
from studentassistant.vault.secrets import redact_secrets

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")


def redact(text: str) -> str:
    """Remove credentials from `text`: the userinfo of any URL and anything shaped like a token."""
    return redact_secrets(_URL_CREDENTIALS.sub(r"\g<scheme>***@", text))


@dataclass(frozen=True)
class GitIdentity:
    """Who a vault commit or tag is authored and committed as."""

    name: str
    email: str


@dataclass(frozen=True)
class GitResult:
    """The outcome of one git command, its output already redacted."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def describe(self) -> str:
        """A one-line account of a failure, safe to log or show."""
        if self.timed_out:
            return f"git {self.args[0]} timed out"
        detail = (self.stderr or self.stdout).strip().splitlines()
        return f"git {self.args[0]} exited {self.returncode}" + (
            f": {detail[-1]}" if detail else ""
        )


class GitCommandError(VaultError):
    """A git command the vault needed to succeed failed; the message is already redacted."""

    def __init__(self, result: GitResult) -> None:
        super().__init__(result.describe())
        self.result = result


class GitRunner:
    """Runs git in one vault root as one identity."""

    def __init__(
        self,
        root: Path,
        identity: GitIdentity,
        timeout: float = 120.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.identity = identity
        self.timeout = timeout
        # Extra variables for every command (how git authenticates to GitHub, `vault/github.py`):
        # a credential reaches git only this way, never through a URL or the repository's config.
        self.extra_environment = dict(environment or {})

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            GIT_AUTHOR_NAME=self.identity.name,
            GIT_AUTHOR_EMAIL=self.identity.email,
            GIT_COMMITTER_NAME=self.identity.name,
            GIT_COMMITTER_EMAIL=self.identity.email,
            GIT_TERMINAL_PROMPT="0",
            # Messages are parsed to tell offline from auth from rejected: keep them in English.
            LC_ALL="C",
            LANG="C",
        )
        environment.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        environment.update(self.extra_environment)
        return environment

    def run(self, *args: str, timeout: float | None = None) -> GitResult:
        """Run `git <args>` in the vault root; never raises for a failing or hanging command."""
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=self._environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return GitResult(args=args, returncode=-1, stdout="", stderr="", timed_out=True)
        except OSError as error:  # git itself missing, or the root gone
            return GitResult(args=args, returncode=-1, stdout="", stderr=redact(str(error)))
        return GitResult(
            args=args,
            returncode=completed.returncode,
            stdout=redact(completed.stdout),
            stderr=redact(completed.stderr),
        )

    def check(self, *args: str, timeout: float | None = None) -> GitResult:
        """Run `git <args>` and raise `GitCommandError` when it fails."""
        result = self.run(*args, timeout=timeout)
        if not result.ok:
            raise GitCommandError(result)
        return result
