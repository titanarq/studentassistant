"""`create_vault`: a new private repository, the vault initialised, committed and pushed to it.

The "GitHub" is a set of bare repositories under `tmp_path` (`github_fakes`); the token run goes
through the real `TokenHost` over `file://` URLs, so the token takes the path a real one would.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import commit_count, git
from secret_samples import GITHUB_PAT

from github_fakes import LocalHost, bare_repo, remote_base
from studentassistant.vault import Vault
from studentassistant.vault.github import GitHubHostError, TokenHost
from studentassistant.vault.setup import SetupError, create_vault

REPO = "ana/vault"
STUDENT = "Ana García"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Git reads the global configuration of whoever runs it: keep it inside `tmp_path`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def github(tmp_path: Path) -> Path:
    return tmp_path / "github"


@pytest.fixture
def host(github: Path) -> LocalHost:
    return LocalHost(github)


def test_create_makes_a_private_repo_and_pushes_the_initial_commit(
    tmp_path: Path, host: LocalHost, github: Path
) -> None:
    result = create_vault(tmp_path / "vault", REPO, STUDENT, host)

    assert result.action == "created"
    assert host.created == [REPO]
    assert result.vault.meta.student == STUDENT
    origin = github / "ana" / "vault.git"
    assert git(result.vault.path, "remote", "get-url", "origin").strip() == host.remote_url(REPO)
    assert git(origin, "rev-parse", "main") == git(result.vault.path, "rev-parse", "HEAD")
    assert set(git(origin, "ls-tree", "--name-only", "main").split()) == {
        ".gitattributes",
        "vault.yaml",
    }
    assert git(result.vault.path, "status", "--porcelain") == ""


def test_create_uses_an_existing_empty_repository(
    tmp_path: Path, host: LocalHost, github: Path
) -> None:
    origin = bare_repo(github, REPO)

    result = create_vault(tmp_path / "vault", REPO, STUDENT, host)

    assert result.action == "created"
    assert host.created == []
    assert commit_count(origin, "main") == 1


def test_create_refuses_a_repository_that_already_has_commits(
    tmp_path: Path, host: LocalHost, github: Path
) -> None:
    create_vault(tmp_path / "first", REPO, STUDENT, host)

    with pytest.raises(SetupError, match="ya existe en GitHub y no está vacío"):
        create_vault(tmp_path / "second", REPO, STUDENT, host)
    assert not (tmp_path / "second").exists()


def test_create_refuses_a_non_empty_directory(tmp_path: Path, host: LocalHost) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    (target / "algo.txt").write_text("de otra persona")

    with pytest.raises(SetupError, match="no está vacío"):
        create_vault(target, REPO, STUDENT, host)
    assert host.created == []


def test_create_accepts_an_empty_directory(tmp_path: Path, host: LocalHost) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    assert create_vault(target, REPO, STUDENT, host).action == "created"


def test_create_rejects_a_malformed_repo_name(tmp_path: Path, host: LocalHost) -> None:
    with pytest.raises(SetupError, match="propietario/nombre"):
        create_vault(tmp_path / "vault", "sin-barra", STUDENT, host)


def test_create_twice_with_the_same_answers_changes_nothing(
    tmp_path: Path, host: LocalHost, github: Path
) -> None:
    first = create_vault(tmp_path / "vault", REPO, STUDENT, host)
    head = git(first.vault.path, "rev-parse", "HEAD")

    again = create_vault(tmp_path / "vault", REPO, STUDENT, host)

    assert again.action == "already-set-up"
    assert host.created == [REPO]
    assert git(again.vault.path, "rev-parse", "HEAD") == head
    assert commit_count(github / "ana" / "vault.git", "main") == 1


def test_create_rerun_pushes_a_vault_whose_first_push_never_happened(
    tmp_path: Path, host: LocalHost, github: Path
) -> None:
    origin = bare_repo(github, REPO)
    vault = Vault.init(tmp_path / "vault", STUDENT)
    git(vault.path, "add", "--all")
    git(vault.path, "commit", "-m", "vault creado")
    git(vault.path, "remote", "add", "origin", host.remote_url(REPO))

    result = create_vault(vault.path, REPO, STUDENT, host)

    assert result.action == "already-set-up"
    assert commit_count(origin, "main") == 1


def test_create_without_push_access_is_reported(tmp_path: Path, host: LocalHost) -> None:
    class ReadOnlyHost(LocalHost):
        def create_private_repo(self, repo: str) -> None:
            super().create_private_repo(repo)
            hooks = self.root / f"{repo}.git" / "hooks" / "pre-receive"
            hooks.write_text("#!/bin/sh\necho denied >&2\nexit 1\n")
            hooks.chmod(0o755)

    with pytest.raises(SetupError, match="no se pudo subir el vault a GitHub"):
        create_vault(tmp_path / "vault", REPO, STUDENT, ReadOnlyHost(host.root))


def test_create_with_a_token_leaves_it_nowhere_on_disk(tmp_path: Path, github: Path) -> None:
    bare_repo(github, REPO)  # a token cannot create: the student made it empty by hand
    host = TokenHost(GITHUB_PAT, remote_base=remote_base(github))
    config = tmp_path / "config.toml"

    result = create_vault(tmp_path / "vault", REPO, STUDENT, host)

    assert result.action == "created"
    written = [p for p in (tmp_path / "vault").rglob("*") if p.is_file()]
    written += [p for p in github.rglob("*") if p.is_file()]
    assert (tmp_path / "vault" / ".git" / "config") in written
    for path in [*written, *([config] if config.exists() else [])]:
        assert GITHUB_PAT.encode() not in path.read_bytes(), path


def test_create_with_a_token_and_no_repository_explains_what_to_do(
    tmp_path: Path, github: Path
) -> None:
    host = TokenHost(GITHUB_PAT, remote_base=remote_base(github))
    github.mkdir()

    with pytest.raises(GitHubHostError, match="crea tú a mano un repositorio privado") as error:
        create_vault(tmp_path / "vault", REPO, STUDENT, host)
    assert GITHUB_PAT not in str(error.value)
    assert not (tmp_path / "vault").exists()


def test_create_through_a_logged_in_gh(
    tmp_path: Path, github: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from github_fakes import install_fake_gh
    from studentassistant.vault.github import GhCliHost, select_host

    env: dict[str, str] = {}
    log = tmp_path / "gh.log"
    install_fake_gh(tmp_path / "bin", github, log, env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    (github / ".authenticated").touch()
    host = select_host({}, remote_base=remote_base(github))
    assert isinstance(host, GhCliHost)

    result = create_vault(tmp_path / "vault", REPO, STUDENT, host)

    assert result.action == "created"
    assert "repo create ana/vault --private" in log.read_text()
    assert commit_count(github / "ana" / "vault.git", "main") == 1
