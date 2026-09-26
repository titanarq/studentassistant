"""The vault's own git credential helper, so a push works without an interactive shell (#308).

`setup` and `doctor` hand git a credential only through the environment of their own git child
processes (`GitHubHost.git_environment()`); the backend's `GitSync` runs git with none, as a
systemd service whose `PATH` may not even hold `gh`. So when the host can name a helper that
works on its own (`GhCliHost`: `gh auth git-credential` at the absolute path resolved now), it is
written to the vault's own `.git/config`, scoped to the GitHub URL:

    [credential "https://github.com"]
        helper =
        helper = !'/abs/path/gh' auth git-credential

The empty first value resets whatever helpers the user's global configuration lists for that URL,
so only this one answers. The helper is a command, never a credential: no token is written to any
file (a `TokenHost` names none; its token stays in the environment of the process that has it).

`probe_unattended_access` runs `git ls-remote` the way the service does -- no host environment,
no terminal prompt, no GUI askpass, a minimal `PATH` -- which is what `doctor` checks.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from studentassistant.vault.git import GitIdentity, GitResult, GitRunner

REMOTE = "origin"
# `git config` exits 1 when a key has no value, 5 when `--unset-all` finds nothing to unset.
_NO_VALUE_EXIT_CODES = (1, 5)
# The directories a systemd user service's `PATH` holds at least (plus the one git is in).
UNATTENDED_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


@dataclass(frozen=True)
class CredentialHelper:
    """A git credential helper for every remote under `base` (e.g. `https://github.com`)."""

    base: str
    command: str

    @property
    def key(self) -> str:
        return helper_key(self.base)


def helper_key(base: str) -> str:
    """The git configuration key of the credential helpers for URLs under `base`."""
    return f"credential.{base.rstrip('/')}.helper"


def configured_helpers(runner: GitRunner, base: str) -> list[str] | None:
    """The helpers the vault's own `.git/config` lists for `base`, in order; `None` when git
    cannot read it (an empty list when there is none)."""
    result = runner.run("config", "--local", "--get-all", helper_key(base))
    if result.ok:
        return result.stdout.splitlines()
    if result.returncode in _NO_VALUE_EXIT_CODES and not result.timed_out:
        return []
    return None


def install_credential_helper(runner: GitRunner, helper: CredentialHelper) -> bool:
    """Make the vault's `.git/config` list exactly `""` then `helper.command` for `helper.base`.

    Returns whether anything changed (an already right configuration is left alone).

    Raises:
        GitCommandError: git could not read or write the repository's configuration.
    """
    wanted = ["", helper.command]
    if configured_helpers(runner, helper.base) == wanted:
        return False
    result = runner.run("config", "--local", "--unset-all", helper.key)
    if not result.ok and result.returncode not in _NO_VALUE_EXIT_CODES:
        runner.check("config", "--local", "--unset-all", helper.key)  # raises with the detail
    for value in wanted:
        runner.check("config", "--local", "--add", helper.key, value)
    return True


def unattended_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The variables that make a git child process behave as under the systemd service.

    No credential helper from the environment (`GIT_CONFIG_COUNT=0`), no terminal prompt, no
    askpass program (an empty `GIT_ASKPASS` stops git looking at `core.askPass` and
    `SSH_ASKPASS`), and a `PATH` of the system directories plus the one git itself is in, so a
    helper that only works because the shell's `PATH` finds `gh` fails here too.
    """
    environ = dict(os.environ if environ is None else environ)
    git = shutil.which("git", path=environ.get("PATH"))
    directories = [str(Path(git).parent)] if git else []
    directories += [d for d in UNATTENDED_PATH if d not in directories]
    return {
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "PATH": os.pathsep.join(directories),
    }


def unattended_runner(root: Path, identity: GitIdentity, timeout: float) -> GitRunner:
    """A runner for `root` that authenticates only the way the service would."""
    return GitRunner(root, identity, timeout=timeout, environment=unattended_environment())


def probe_unattended_access(runner: GitRunner, remote: str = REMOTE) -> GitResult:
    """`git ls-remote --heads <remote>` through `runner` (use `unattended_runner`)."""
    return runner.run("ls-remote", "--heads", remote)
