"""`studentassistant setup`'s two flows: create a new vault on GitHub, or clone an existing one.

ADR-0002's path to a working PC is "install app -> setup -> clone vault -> index rebuild". Either
flow ends with a vault at `path` whose `origin` is the GitHub repository and whose push access has
been checked, so the first real push of a session does not discover a missing permission.

Both are idempotent: a vault already at `path` whose `origin` is the same repository is accepted as
set up (only the push access is checked again, and a create interrupted before its push is pushed),
so re-running `setup` with the same answers changes nothing.

A vault an older installation pushed -- format 1, its content at the repository root -- is cloned
without complaint, and the flow ends with `warn` given a Spanish message naming
`studentassistant vault migrate-users`, the one command that moves that content into its first
user's folder (#548). Refusing it instead would leave a student who has just brought their notes to
a new PC with a directory they may not use and no way to find out why.

GitHub is reached through a `GitHubHost` (`vault/github.py`); during setup the credential a git
command needs reaches it through the host's environment. So that the backend (a systemd service,
without that environment) can push too, every flow writes the host's credential helper -- a
command such as `gh auth git-credential` at its absolute path, never a secret -- to the vault's own
`.git/config` (`vault/credentials.py`, #308) and checks push access without the host's
environment. Errors are a `SetupError` with a Spanish message the command line shows as it is.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from studentassistant.config import DEFAULT_VAULT_AUTHOR_EMAIL, check_repo_name
from studentassistant.vault.credentials import install_credential_helper, unattended_runner
from studentassistant.vault.errors import VaultError
from studentassistant.vault.git import GitCommandError, GitIdentity, GitResult, GitRunner
from studentassistant.vault.github import GitHubHost
from studentassistant.vault.vault import (
    MAIN_BRANCH,
    Vault,
    VaultFormatError,
    VaultMetaError,
    VaultNeedsMigrationError,
)

REMOTE = "origin"
INITIAL_COMMIT_MESSAGE = "vault creado"
DEFAULT_SETUP_TIMEOUT_SECONDS = 120.0
NOT_KNOWN_PRIVATE_WARNING = (
    "Aviso: sin `gh` no se puede comprobar que el repositorio {repo} sea privado. Confirma en"
    " GitHub que lo es: el vault guarda tus apuntes y tus clases."
)

SetupAction = Literal["created", "cloned", "already-set-up"]
PostCloneHook = Callable[[Vault], None]
Warn = Callable[[str], None]


class SetupError(VaultError):
    """`setup` could not finish; the message is Spanish, redacted, and says what went wrong."""


@dataclass(frozen=True)
class SetupResult:
    """What `setup` left behind: the vault, the repository it pushes to, and what it did."""

    vault: Vault
    repo: str
    action: SetupAction


def _no_hook(vault: Vault) -> None:
    """The default post-clone hook: nothing (the CLI passes the index rebuild)."""


def _no_warning(message: str) -> None:
    """The default `warn`: nobody to tell."""


def _check_repo(repo: str) -> None:
    try:
        check_repo_name(repo)
    except ValueError as error:
        raise SetupError(
            f"«{repo}» no es un repositorio de GitHub válido: escríbelo como propietario/nombre"
        ) from error


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return Path("/")


def _runner(root: Path, host: GitHubHost, identity: GitIdentity, timeout: float) -> GitRunner:
    return GitRunner(root, identity, timeout=timeout, environment=host.git_environment())


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return re.sub(r"\.git$", "", url)


def _is_set_up(path: Path, host: GitHubHost, repo: str, identity: GitIdentity) -> bool:
    """Whether `path` is already a git repository whose `origin` is `repo`."""
    if not (path / ".git").exists():
        return False
    result = GitRunner(path, identity).run("remote", "get-url", REMOTE)
    return result.ok and _normalize_url(result.stdout) == _normalize_url(host.remote_url(repo))


def _refuse_non_empty(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise SetupError(
            f"{path} ya existe y no está vacío, y no es un vault de este repositorio:"
            " elige otra carpeta"
        )


def _fail(what: str, result: GitResult) -> SetupError:
    return SetupError(f"{what}: {result.describe()}")


def _remote_has_commits(runner: GitRunner, url: str) -> bool:
    result = runner.run("ls-remote", "--heads", url)
    if not result.ok:
        raise _fail("no se pudo leer el repositorio de GitHub", result)
    return bool(result.stdout.strip())


def verify_push_access(runner: GitRunner) -> None:
    """Check that the vault may push to `origin` (`git push --dry-run`), or raise `SetupError`."""
    result = runner.run("push", "--dry-run", REMOTE, MAIN_BRANCH)
    if not result.ok:
        raise _fail(
            "no hay permiso para subir cambios al repositorio de GitHub"
            " (revisa que tu cuenta o tu token pueda escribir en él)",
            result,
        )


def ensure_credential_helper(
    path: Path,
    host: GitHubHost,
    author_email: str = DEFAULT_VAULT_AUTHOR_EMAIL,
    timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
) -> bool | None:
    """Write `host`'s credential helper to the vault's `.git/config` at `path` (#308).

    Returns whether the configuration changed, or `None` when the host has no helper to persist
    (a token host: the token stays in the environment). `setup` calls it; `doctor --fix` repairs
    a vault set up before it existed with it.

    Raises:
        SetupError: git could not write the repository's configuration (Spanish message).
    """
    helper = host.credential_helper()
    if helper is None:
        return None
    runner = GitRunner(path, _identity("Student Assistant", author_email), timeout=timeout)
    try:
        return install_credential_helper(runner, helper)
    except GitCommandError as error:
        raise SetupError(
            f"no se pudo guardar en el vault cómo se conecta git a GitHub: {error}"
        ) from error


def _persist_and_verify(
    path: Path, host: GitHubHost, identity: GitIdentity, timeout: float
) -> None:
    """Persist the host's helper, then check push access the way the backend will push: without
    the host's environment when a helper was persisted, with it otherwise."""
    if ensure_credential_helper(path, host, identity.email, timeout) is None:
        verify_push_access(_runner(path, host, identity, timeout))
    else:
        verify_push_access(unattended_runner(path, identity, timeout))


def _open(path: Path, warn: Warn = _no_warning) -> Vault:
    """Open the vault `setup` has just created, cloned, or found already set up at `path`.

    A format-1 vault is not refused: `setup --clone` of a repository an older installation pushed
    leaves it in place, opened for migration, with `warn` told to say so in Spanish, because a
    student who has just cloned their own notes should end the flow with them on disk and the one
    command that unlocks them, not with a refusal (#548). What is still refused is a format this
    backend cannot read at all -- a vault a newer backend wrote -- and the clone is left in place
    for that newer backend.

    Raises:
        SetupError: when `vault.yaml` declares a format this backend cannot read, or is missing or
            unreadable (Spanish message).
    """
    try:
        return Vault.open(path)
    except VaultNeedsMigrationError as error:
        warn(f"Aviso: {error}")
        return Vault.open_for_migration(path)
    except VaultFormatError as error:
        raise SetupError(
            f"el vault de {path} es de una versión de formato que esta aplicación no entiende:"
            " actualiza Student Assistant y vuelve a lanzar `setup` (el clon se queda donde está)"
        ) from error
    except VaultMetaError as error:
        raise SetupError(
            f"{path} no contiene un vault de Student Assistant (falta o no se entiende vault.yaml)"
        ) from error


def _identity(student: str, email: str) -> GitIdentity:
    return GitIdentity(name=student, email=email)


def create_vault(
    path: Path,
    repo: str,
    student: str,
    host: GitHubHost,
    author_email: str = DEFAULT_VAULT_AUTHOR_EMAIL,
    timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
    warn: Warn = _no_warning,
) -> SetupResult:
    """Create a new vault at `path` and push it to a new private repository `repo` (`owner/name`).

    What is created is a format-2 vault with its first user already in it, named after `student`
    (`Vault.init`), so the repository pushed to GitHub holds somebody's folder from its first
    commit and the student who just ran `setup --create` can write a subject straight away.

    An existing empty repository is used as it is (that is how a PC without `gh` creates one: by
    hand on GitHub) once it is known to be private: a public one is refused, and when the host
    cannot tell (a token without `gh`) `warn` is given a Spanish message asking the student to
    confirm it. A repository that already has commits is refused. Everything GitHub could refuse
    is checked before anything is written locally.

    Raises:
        SetupError: on any refusal or failure (Spanish message).
    """
    _check_repo(repo)
    path = path.expanduser().absolute()
    identity = _identity(student, author_email)
    url = host.remote_url(repo)

    if _is_set_up(path, host, repo, identity):
        vault = _open(path, warn)
        runner = _runner(path, host, identity, timeout)
        if not _remote_has_commits(runner, url):
            _push(runner)
        _persist_and_verify(path, host, identity, timeout)
        return SetupResult(vault=vault, repo=repo, action="already-set-up")

    _refuse_non_empty(path)
    probe = _runner(_existing_ancestor(path), host, identity, timeout)
    if host.repo_exists(repo):
        if _remote_has_commits(probe, url):
            raise SetupError(
                f"el repositorio {repo} ya existe en GitHub y no está vacío: para usarlo en este"
                " PC elige clonar, o crea un vault nuevo con otro nombre"
            )
        private = host.repo_is_private(repo)
        if private is False:
            raise SetupError(
                f"el repositorio {repo} es público y el vault guarda tus apuntes y tus clases:"
                " hazlo privado en GitHub (Settings > Danger Zone > Change visibility) y vuelve a"
                " lanzar `setup`"
            )
        if private is None:
            warn(NOT_KNOWN_PRIVATE_WARNING.format(repo=repo))
    else:
        host.create_private_repo(repo)

    if path.exists():  # an empty directory: `Vault.init` wants to make it itself
        path.rmdir()
    vault = Vault.init(path, student)
    runner = _runner(path, host, identity, timeout)
    for args in (("add", "--all"), ("commit", "--quiet", "-m", INITIAL_COMMIT_MESSAGE)):
        result = runner.run(*args)
        if not result.ok:
            raise _fail("no se pudo hacer el primer commit del vault", result)
    result = runner.run("remote", "add", REMOTE, url)
    if not result.ok:
        raise _fail("no se pudo registrar el repositorio de GitHub como origin", result)
    _push(runner)
    _persist_and_verify(path, host, identity, timeout)
    return SetupResult(vault=vault, repo=repo, action="created")


def _push(runner: GitRunner) -> None:
    result = runner.run("push", "--set-upstream", REMOTE, MAIN_BRANCH)
    if not result.ok:
        raise _fail("no se pudo subir el vault a GitHub", result)


def clone_vault(
    path: Path,
    repo: str,
    host: GitHubHost,
    post_clone: PostCloneHook = _no_hook,
    author_email: str = DEFAULT_VAULT_AUTHOR_EMAIL,
    timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
    warn: Warn = _no_warning,
) -> SetupResult:
    """Clone the vault `repo` (`owner/name`) into `path`, check it, then call `post_clone` once.

    A vault of a `format_version` this backend cannot read at all is refused, and the clone is left
    in place for a newer backend. A format-1 one is not: the clone succeeds, the handle it returns
    is the migration's, and `warn` is given the Spanish message that names
    `studentassistant vault migrate-users` (#548). `post_clone` is where the index rebuild plugs in;
    it is not called when `path` already held this vault (nothing was cloned).

    Raises:
        SetupError: on any refusal or failure (Spanish message).
    """
    _check_repo(repo)
    path = path.expanduser().absolute()
    identity = _identity("Student Assistant", author_email)
    url = host.remote_url(repo)

    if _is_set_up(path, host, repo, identity):
        vault = _open(path, warn)
        _persist_and_verify(path, host, identity, timeout)
        return SetupResult(vault=vault, repo=repo, action="already-set-up")

    _refuse_non_empty(path)
    if not host.repo_exists(repo):
        raise SetupError(
            f"el repositorio {repo} no existe en GitHub o tu cuenta no tiene acceso a él"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _runner(path.parent, host, identity, timeout).run("clone", "--quiet", url, str(path))
    if not result.ok:
        raise _fail(f"no se pudo clonar {repo}", result)
    vault = _open(path, warn)
    _persist_and_verify(path, host, identity, timeout)
    post_clone(vault)
    return SetupResult(vault=vault, repo=repo, action="cloned")


@dataclass(frozen=True)
class RemoteAccess:
    """What `check_remote_access` found about the vault's `origin`.

    `url` is redacted; `repo_matches` says whether it is the configured repository (`None` when
    there was none to compare with); `push_error` is `None` when `git push --dry-run` succeeded,
    otherwise a Spanish, redacted account of why it did not.
    """

    url: str
    repo_matches: bool | None
    push_error: str | None


def check_remote_access(
    path: Path,
    host: GitHubHost | None,
    repo: str | None = None,
    author_email: str = DEFAULT_VAULT_AUTHOR_EMAIL,
    timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
) -> RemoteAccess:
    """Look at the vault's `origin` for `studentassistant doctor`: which it is, may we push to it.

    `host` supplies how git authenticates (none: git's own configuration, e.g. SSH) and how
    `repo` (`owner/name`) is written as a URL. Nothing is written: the push is a dry run.

    Raises:
        SetupError: `path` is no git repository or has no `origin` (Spanish message).
    """
    path = path.expanduser().absolute()
    if not (path / ".git").exists():
        raise SetupError(f"{path} no es un repositorio git: lanza `studentassistant setup`")
    identity = _identity("Student Assistant", author_email)
    environment = host.git_environment() if host is not None else {}
    runner = GitRunner(path, identity, timeout=timeout, environment=environment)
    result = runner.run("remote", "get-url", REMOTE)
    if not result.ok or not result.stdout.strip():
        raise SetupError("el vault no tiene `origin`: lanza `studentassistant setup`")
    url = result.stdout.strip()
    matches: bool | None = None
    if repo is not None and host is not None:
        matches = _normalize_url(url) == _normalize_url(host.remote_url(repo))
    try:
        verify_push_access(runner)
    except SetupError as error:
        return RemoteAccess(url=url, repo_matches=matches, push_error=str(error))
    return RemoteAccess(url=url, repo_matches=matches, push_error=None)
