"""How `studentassistant setup` reaches GitHub: the `gh` CLI, or a token taken from the environment.

The vault module has no HTTP client (docs/modules/vault.md, "Boundaries"), so every GitHub
operation here is a subprocess: `gh` for what only the GitHub API can do (does a repository exist,
create a private one) and `git` for the rest. Two hosts implement the `GitHubHost` protocol:

- `GhCliHost`, used when `gh auth status` succeeds: `gh` holds the credential, and git borrows it
  through `gh auth git-credential` as its credential helper.
- `TokenHost`, used otherwise when a fine-grained token is in the environment (`GH_TOKEN` or
  `GITHUB_TOKEN`). The token is handed to git only as an environment variable a credential helper
  given on the command line reads, so it never lands in a remote URL, `.git/config`, the vault or
  the configuration file. Without `gh` there is no way to *create* a repository (that is a REST
  call), so `create_private_repo` refuses with what to do instead.

Whatever `gh` or `git` prints is passed through `redact` before it reaches an error message.
Messages are Spanish: `setup` shows them to the student as they are.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Protocol

from studentassistant.vault.errors import VaultError
from studentassistant.vault.git import redact

GITHUB_URL = "https://github.com"
# Where `TokenHost` looks for the token, first set wins (`GH_TOKEN` is also what `gh` reads).
TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
# The variable the token travels in to a git child process; its credential helper reads it.
GIT_TOKEN_ENV_VAR = "STUDENTASSISTANT_GIT_TOKEN"
DEFAULT_HOST_TIMEOUT_SECONDS = 120.0

NO_GH_CREATE_MESSAGE = (
    "Sin la herramienta `gh` no se puede crear un repositorio en GitHub. Instala `gh` y ejecuta"
    " `gh auth login`, o crea tú a mano un repositorio privado y vacío en GitHub y vuelve a lanzar"
    " `studentassistant setup --create`: el vault se subirá a ese repositorio."
)
NO_CREDENTIALS_MESSAGE = (
    "No hay forma de acceder a GitHub: `gh` no está instalado o no ha iniciado sesión"
    " (`gh auth login`), y no hay ningún token en las variables de entorno "
    + " ni ".join(TOKEN_ENV_VARS)
    + "."
)


class GitHubHostError(VaultError):
    """A GitHub operation failed; the message is Spanish and already redacted."""


class GitHubHost(Protocol):
    """What `setup` needs from GitHub, whichever way it is reached."""

    name: str

    def authenticated(self) -> bool:
        """Whether this host has a credential to act with."""
        ...

    def remote_url(self, repo: str) -> str:
        """The URL git clones `repo` (`owner/name`) from and pushes to; never holds a secret."""
        ...

    def repo_exists(self, repo: str) -> bool:
        """Whether `repo` exists and is visible with this host's credential."""
        ...

    def repo_is_private(self, repo: str) -> bool | None:
        """Whether the existing `repo` is private; `None` when this host cannot tell."""
        ...

    def create_private_repo(self, repo: str) -> None:
        """Create `repo` as an empty private repository."""
        ...

    def git_environment(self) -> dict[str, str]:
        """The variables a git child process needs to authenticate against the remote."""
        ...


def _credential_helper_environment(helper: str) -> dict[str, str]:
    """Git configuration given through the environment: reset the helpers, then use `helper`.

    `GIT_CONFIG_COUNT` entries apply to this process only; nothing is written to any config file.
    """
    key = f"credential.{GITHUB_URL}.helper"
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": key,
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": key,
        "GIT_CONFIG_VALUE_1": helper,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run(
    command: list[str], environment: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run `command` without a terminal; its output comes back redacted."""
    try:
        completed = subprocess.run(
            command,
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired as error:
        raise GitHubHostError(f"`{command[0]}` no respondió a tiempo") from error
    except OSError as error:
        detail = redact(str(error))
        raise GitHubHostError(f"no se pudo ejecutar `{command[0]}`: {detail}") from error
    return subprocess.CompletedProcess(
        completed.args, completed.returncode, redact(completed.stdout), redact(completed.stderr)
    )


def _last_line(completed: subprocess.CompletedProcess[str]) -> str:
    lines = (completed.stderr or completed.stdout).strip().splitlines()
    return lines[-1] if lines else f"código de salida {completed.returncode}"


class GhCliHost:
    """GitHub through the `gh` CLI and the credential it holds."""

    name = "gh"

    def __init__(
        self,
        gh: str = "gh",
        remote_base: str = GITHUB_URL,
        timeout: float = DEFAULT_HOST_TIMEOUT_SECONDS,
    ) -> None:
        self.gh = gh
        self.remote_base = remote_base.rstrip("/")
        self.timeout = timeout

    def _gh(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
        return _run([self.gh, *args], environment, self.timeout)

    def authenticated(self) -> bool:
        if shutil.which(self.gh) is None:
            return False
        try:
            return self._gh("auth", "status").returncode == 0
        except GitHubHostError:
            return False

    def remote_url(self, repo: str) -> str:
        return f"{self.remote_base}/{repo}.git"

    def repo_exists(self, repo: str) -> bool:
        completed = self._gh("api", f"repos/{repo}", "--silent")
        if completed.returncode == 0:
            return True
        if "404" in completed.stderr or "Not Found" in completed.stderr:
            return False
        raise GitHubHostError(
            f"no se pudo comprobar si existe {repo} en GitHub: {_last_line(completed)}"
        )

    def repo_is_private(self, repo: str) -> bool:
        completed = self._gh("repo", "view", repo, "--json", "visibility", "--jq", ".visibility")
        if completed.returncode != 0:
            raise GitHubHostError(
                f"no se pudo comprobar la visibilidad de {repo} en GitHub: {_last_line(completed)}"
            )
        return completed.stdout.strip().upper() == "PRIVATE"

    def create_private_repo(self, repo: str) -> None:
        completed = self._gh("repo", "create", repo, "--private")
        if completed.returncode != 0:
            raise GitHubHostError(
                f"no se pudo crear el repositorio privado {repo} en GitHub: {_last_line(completed)}"
            )

    def git_environment(self) -> dict[str, str]:
        gh = shutil.which(self.gh) or self.gh
        return _credential_helper_environment(f"!{shlex.quote(gh)} auth git-credential")


class TokenHost:
    """GitHub through a fine-grained token and `git` alone: clone and push, but never create."""

    name = "token"

    def __init__(
        self,
        token: str,
        remote_base: str = GITHUB_URL,
        timeout: float = DEFAULT_HOST_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self.remote_base = remote_base.rstrip("/")
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token, not even in a traceback
        return f"TokenHost(remote_base={self.remote_base!r})"

    def authenticated(self) -> bool:
        return bool(self._token)

    def remote_url(self, repo: str) -> str:
        return f"{self.remote_base}/{repo}.git"

    def repo_exists(self, repo: str) -> bool:
        environment = {**os.environ, "LC_ALL": "C", "LANG": "C", **self.git_environment()}
        completed = _run(
            ["git", "ls-remote", "--heads", self.remote_url(repo)], environment, self.timeout
        )
        if completed.returncode == 0:
            return True
        detail = completed.stderr.lower()
        if "not found" in detail or "does not appear to be a git repository" in detail:
            return False
        raise GitHubHostError(
            f"no se pudo comprobar si existe {repo} en GitHub: {_last_line(completed)}"
        )

    def repo_is_private(self, repo: str) -> None:
        return None  # only the GitHub API knows, and this host has no way to ask it

    def create_private_repo(self, repo: str) -> None:
        raise GitHubHostError(NO_GH_CREATE_MESSAGE)

    def git_environment(self) -> dict[str, str]:
        helper = (
            '!f() { test "$1" = get || return 0; echo username=x-access-token;'
            f' echo "password=${GIT_TOKEN_ENV_VAR}"; }}; f'
        )
        return {**_credential_helper_environment(helper), GIT_TOKEN_ENV_VAR: self._token}


def token_from_environment(environ: Mapping[str, str] | None = None) -> str | None:
    """The first non-empty token among `TOKEN_ENV_VARS`, or `None`."""
    environ = os.environ if environ is None else environ
    for name in TOKEN_ENV_VARS:
        if environ.get(name):
            return environ[name]
    return None


def select_host(
    environ: Mapping[str, str] | None = None, gh: str = "gh", remote_base: str = GITHUB_URL
) -> GitHubHost:
    """`gh` when `gh auth status` succeeds, otherwise the token in the environment.

    Raises:
        GitHubHostError: when there is neither (Spanish message saying what to do).
    """
    gh_host = GhCliHost(gh=gh, remote_base=remote_base)
    if gh_host.authenticated():
        return gh_host
    token = token_from_environment(environ)
    if token:
        return TokenHost(token, remote_base=remote_base)
    raise GitHubHostError(NO_CREDENTIALS_MESSAGE)
