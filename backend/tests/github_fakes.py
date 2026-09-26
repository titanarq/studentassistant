"""A local stand-in for GitHub: bare repositories under `tmp_path`, a fake host and a fake `gh`.

No setup test reaches the network or the real GitHub: a repository `owner/name` is the bare
repository `<root>/owner/name.git`, and its URL is a `file://` one.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from studentassistant.vault.credentials import CredentialHelper


def remote_base(root: Path) -> str:
    return f"file://{root}"


def bare_repo(root: Path, repo: str) -> Path:
    """Create `repo` as an empty bare repository under `root` (a hand-made empty GitHub repo)."""
    path = root / f"{repo}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class LocalHost:
    """A `GitHubHost` over bare repositories under `root`; records what it was asked."""

    name = "local"

    def __init__(
        self,
        root: Path,
        authenticated: bool = True,
        private: bool | None = True,
        helper: CredentialHelper | None = None,
    ) -> None:
        self.root = root
        self._authenticated = authenticated
        self.private = private
        self.helper = helper
        self.created: list[str] = []

    def authenticated(self) -> bool:
        return self._authenticated

    def remote_url(self, repo: str) -> str:
        return f"{remote_base(self.root)}/{repo}.git"

    def repo_exists(self, repo: str) -> bool:
        return (self.root / f"{repo}.git").is_dir()

    def repo_is_private(self, repo: str) -> bool | None:
        return self.private

    def create_private_repo(self, repo: str) -> None:
        self.created.append(repo)
        bare_repo(self.root, repo)

    def git_environment(self) -> dict[str, str]:
        return {}

    def credential_helper(self) -> CredentialHelper | None:
        return self.helper


FAKE_GH = """#!/usr/bin/env bash
# A fake `gh`: logs its arguments, answers from the bare repositories under $FAKE_GH_ROOT.
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "auth status")
    if [ -e "$FAKE_GH_ROOT/.authenticated" ]; then echo "Logged in"; exit 0; fi
    echo "You are not logged into any GitHub hosts. token=$GH_TOKEN" >&2; exit 1 ;;
  "api repos/"*)
    repo="${2#repos/}"
    if [ -e "$FAKE_GH_ROOT/.broken" ]; then echo "HTTP 500: boom token=$GH_TOKEN" >&2; exit 1; fi
    if [ -d "$FAKE_GH_ROOT/$repo.git" ]; then exit 0; fi
    echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  "repo view")
    if [ "$4 $5 $6 $7" != "--json visibility --jq .visibility" ]; then echo "bad $*" >&2; exit 2; fi
    if [ ! -d "$FAKE_GH_ROOT/$3.git" ]; then echo "Could not resolve" >&2; exit 1; fi
    if [ -e "$FAKE_GH_ROOT/$3.git/PUBLIC" ]; then echo PUBLIC; else echo PRIVATE; fi ;;
  "repo create")
    if [ "$4" != "--private" ]; then echo "not private" >&2; exit 1; fi
    if [ -e "$FAKE_GH_ROOT/.broken" ]; then echo "HTTP 500: boom token=$GH_TOKEN" >&2; exit 1; fi
    mkdir -p "$(dirname "$FAKE_GH_ROOT/$3")"
    git init --bare --quiet -b main "$FAKE_GH_ROOT/$3.git" ;;
  "auth git-credential")
    exit 0 ;;
  *) echo "fake gh: unexpected $*" >&2; exit 2 ;;
esac
"""


def install_fake_gh(bin_dir: Path, root: Path, log: Path, env: dict[str, str]) -> Path:
    """Write the fake `gh` into `bin_dir` and fill `env` with what it reads (PATH included)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_GH_ROOT"] = str(root)
    env["FAKE_GH_LOG"] = str(log)
    return gh
