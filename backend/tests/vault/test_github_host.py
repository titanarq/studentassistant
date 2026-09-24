"""The GitHub hosts: `gh` when it is logged in, a token from the environment otherwise.

`gh` is a fake script on `PATH` answering from bare repositories under `tmp_path`; nothing here
reaches the network. The token never shows up in an error, and reaches git only through the
environment of the child process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from secret_samples import GITHUB_PAT

from github_fakes import bare_repo, install_fake_gh, remote_base
from studentassistant.vault.github import (
    GIT_TOKEN_ENV_VAR,
    NO_GH_CREATE_MESSAGE,
    GhCliHost,
    GitHubHostError,
    TokenHost,
    select_host,
    token_from_environment,
)


@pytest.fixture
def github_root(tmp_path: Path) -> Path:
    return tmp_path / "github"


@pytest.fixture
def gh_log(tmp_path: Path) -> Path:
    return tmp_path / "gh.log"


@pytest.fixture
def fake_gh(
    tmp_path: Path, github_root: Path, gh_log: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env: dict[str, str] = {}
    gh = install_fake_gh(tmp_path / "bin", github_root, gh_log, env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return gh


def login(github_root: Path) -> None:
    (github_root / ".authenticated").touch()


def test_gh_host_is_authenticated_only_when_gh_auth_status_succeeds(
    fake_gh: Path, github_root: Path
) -> None:
    host = GhCliHost()
    assert not host.authenticated()
    login(github_root)
    assert host.authenticated()


def test_gh_host_is_not_authenticated_without_gh(tmp_path: Path) -> None:
    assert not GhCliHost(gh=str(tmp_path / "no-such-gh")).authenticated()


def test_gh_host_tells_an_existing_repo_from_a_missing_one(
    fake_gh: Path, github_root: Path
) -> None:
    bare_repo(github_root, "ana/vault")
    host = GhCliHost()
    assert host.repo_exists("ana/vault")
    assert not host.repo_exists("ana/otro")


def test_gh_host_creates_the_repository_private(
    fake_gh: Path, github_root: Path, gh_log: Path
) -> None:
    host = GhCliHost()
    host.create_private_repo("ana/vault")
    assert (github_root / "ana" / "vault.git").is_dir()
    assert "repo create ana/vault --private" in gh_log.read_text()


def test_gh_host_errors_are_spanish_and_redacted(
    fake_gh: Path, github_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", GITHUB_PAT)
    (github_root / ".broken").touch()
    host = GhCliHost()
    with pytest.raises(GitHubHostError) as exists_error:
        host.repo_exists("ana/vault")
    with pytest.raises(GitHubHostError) as create_error:
        host.create_private_repo("ana/vault")
    for error in (exists_error, create_error):
        assert "no se pudo" in str(error.value)
        assert GITHUB_PAT not in str(error.value)


def test_gh_host_lends_git_its_credential_through_the_environment(fake_gh: Path) -> None:
    environment = GhCliHost().git_environment()
    assert environment["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert environment["GIT_CONFIG_VALUE_1"].endswith("auth git-credential")
    assert environment["GIT_CONFIG_VALUE_0"] == ""  # other helpers are reset first


def test_token_host_passes_the_token_only_in_the_environment(tmp_path: Path) -> None:
    host = TokenHost(GITHUB_PAT)
    environment = host.git_environment()
    assert environment[GIT_TOKEN_ENV_VAR] == GITHUB_PAT
    assert all(
        GITHUB_PAT not in value for key, value in environment.items() if key != GIT_TOKEN_ENV_VAR
    )
    assert GITHUB_PAT not in host.remote_url("ana/vault")
    assert GITHUB_PAT not in repr(host)


def test_token_host_credential_helper_answers_git_with_the_token(tmp_path: Path) -> None:
    helper = TokenHost(GITHUB_PAT).git_environment()["GIT_CONFIG_VALUE_1"]
    assert helper.startswith("!")
    answer = subprocess.run(
        ["sh", "-c", helper[1:] + ' "$@"', "helper", "get"],
        env={GIT_TOKEN_ENV_VAR: GITHUB_PAT, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert answer.splitlines() == ["username=x-access-token", f"password={GITHUB_PAT}"]


def test_token_host_checks_existence_with_git_alone(tmp_path: Path) -> None:
    root = tmp_path / "github"
    bare_repo(root, "ana/vault")
    host = TokenHost(GITHUB_PAT, remote_base=remote_base(root))
    assert host.repo_exists("ana/vault")
    assert not host.repo_exists("ana/otro")


def test_token_host_refuses_to_create_and_says_what_to_do() -> None:
    with pytest.raises(GitHubHostError) as error:
        TokenHost(GITHUB_PAT).create_private_repo("ana/vault")
    assert str(error.value) == NO_GH_CREATE_MESSAGE
    assert "gh auth login" in str(error.value)
    assert GITHUB_PAT not in str(error.value)


def test_token_from_environment_takes_the_first_non_empty() -> None:
    assert token_from_environment({"GH_TOKEN": "", "GITHUB_TOKEN": "b"}) == "b"
    assert token_from_environment({"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}) == "a"
    assert token_from_environment({}) is None


def test_select_host_prefers_a_logged_in_gh(fake_gh: Path, github_root: Path) -> None:
    login(github_root)
    assert isinstance(select_host({"GH_TOKEN": GITHUB_PAT}), GhCliHost)


def test_select_host_falls_back_to_the_token(fake_gh: Path) -> None:
    host = select_host({"GITHUB_TOKEN": GITHUB_PAT})
    assert isinstance(host, TokenHost)


def test_select_host_without_gh_nor_token_explains_in_spanish(fake_gh: Path) -> None:
    with pytest.raises(GitHubHostError) as error:
        select_host({})
    assert "No hay forma de acceder a GitHub" in str(error.value)


def test_gh_host_reads_the_visibility_of_a_repository(fake_gh: Path, github_root: Path) -> None:
    bare_repo(github_root, "ana/vault")
    public = bare_repo(github_root, "ana/publico")
    (public / "PUBLIC").touch()
    host = GhCliHost()
    assert host.repo_is_private("ana/vault") is True
    assert host.repo_is_private("ana/publico") is False
    with pytest.raises(GitHubHostError, match="visibilidad"):
        host.repo_is_private("ana/no-existe")


def test_token_host_cannot_tell_the_visibility() -> None:
    assert TokenHost(GITHUB_PAT).repo_is_private("ana/vault") is None
