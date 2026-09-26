"""The vault's own credential helper (#308): what `setup` and `doctor --fix` persist so the systemd
service can push without the shell's `gh` or `PATH`. Temporary repositories only, no network."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from git_helpers import git

from github_fakes import LocalHost, install_fake_gh, remote_base
from studentassistant.vault.credentials import (
    CredentialHelper,
    configured_helpers,
    helper_key,
    install_credential_helper,
    probe_unattended_access,
    unattended_environment,
    unattended_runner,
)
from studentassistant.vault.git import GitIdentity, GitRunner
from studentassistant.vault.github import GITHUB_URL, GhCliHost, TokenHost
from studentassistant.vault.setup import clone_vault, create_vault, ensure_credential_helper

REPO = "ana/vault"
IDENTITY = GitIdentity(name="Student Assistant", email="sa@example.invalid")
HELPER_SCRIPT = """#!/bin/sh
# A credential helper that answers `get` with a fixed, harmless credential.
test "$1" = get || exit 0
echo username={username}
echo password=helper-answer
"""


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No user or system git configuration leaks in; `global` is the test's own global file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    return global_config


def helper_script(directory: Path, username: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"helper-{username}"
    script.write_text(HELPER_SCRIPT.format(username=username), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def repo(tmp_path: Path) -> GitRunner:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(root)], check=True)
    return GitRunner(root, IDENTITY, timeout=30)


def credential_fill(root: Path) -> str:
    """What git answers for a GitHub URL, run as the service would (no prompt, minimal PATH)."""
    completed = subprocess.run(
        ["git", "credential", "fill"],
        cwd=root,
        env={**os.environ, **unattended_environment()},
        input="protocol=https\nhost=github.com\npath=ana/vault.git\n\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout


def test_install_writes_a_reset_then_the_helper_and_is_idempotent(repo: GitRunner) -> None:
    helper = CredentialHelper(GITHUB_URL, "!'/opt/gh' auth git-credential")

    assert configured_helpers(repo, GITHUB_URL) == []
    assert install_credential_helper(repo, helper) is True
    assert configured_helpers(repo, GITHUB_URL) == ["", "!'/opt/gh' auth git-credential"]
    assert install_credential_helper(repo, helper) is False

    replacement = CredentialHelper(GITHUB_URL, "!'/usr/bin/gh' auth git-credential")
    assert install_credential_helper(repo, replacement) is True
    assert git(
        repo.root, "config", "--local", "--get-all", helper_key(GITHUB_URL)
    ).splitlines() == [
        "",
        "!'/usr/bin/gh' auth git-credential",
    ]


def test_the_persisted_helper_answers_without_path_and_shadows_global_helpers(
    tmp_path: Path, repo: GitRunner, isolated_git: Path
) -> None:
    ours = helper_script(tmp_path / "not-on-path", "ours")
    theirs = helper_script(tmp_path / "global", "theirs")
    isolated_git.write_text(f'[credential "{GITHUB_URL}"]\n\thelper = !{theirs}\n')

    assert "username=theirs" in credential_fill(repo.root)

    install_credential_helper(repo, CredentialHelper(GITHUB_URL, f"!{ours}"))

    answer = credential_fill(repo.root)
    assert "username=ours" in answer
    assert "password=helper-answer" in answer
    assert "theirs" not in answer


def test_without_a_helper_git_neither_prompts_nor_answers(repo: GitRunner) -> None:
    assert "password=" not in credential_fill(repo.root)


def test_unattended_environment_drops_askpass_and_the_shell_path(tmp_path: Path) -> None:
    environment = unattended_environment({"PATH": f"{tmp_path}/brew/bin:/usr/bin"})

    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ASKPASS"] == ""
    assert environment["SSH_ASKPASS"] == ""
    assert environment["GIT_CONFIG_COUNT"] == "0"
    assert "brew" not in environment["PATH"]
    assert "/usr/bin" in environment["PATH"].split(os.pathsep)


def test_gh_host_names_its_absolute_path_and_a_token_host_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env: dict[str, str] = {}
    gh = install_fake_gh(tmp_path / "brew" / "bin", tmp_path / "github", tmp_path / "gh.log", env)
    monkeypatch.setenv("PATH", env["PATH"])

    helper = GhCliHost().credential_helper()

    assert helper == CredentialHelper(GITHUB_URL, f"!{gh} auth git-credential")
    assert TokenHost("t" * 20).credential_helper() is None
    monkeypatch.setenv("PATH", "/nonexistent")
    assert GhCliHost().credential_helper() is None


@pytest.fixture
def helper(tmp_path: Path) -> CredentialHelper:
    return CredentialHelper(GITHUB_URL, f"!{helper_script(tmp_path / 'bin', 'gh')}")


def test_create_and_clone_persist_the_hosts_helper(
    tmp_path: Path, helper: CredentialHelper
) -> None:
    host = LocalHost(tmp_path / "github", helper=helper)

    created = create_vault(tmp_path / "pc1", REPO, "Ana", host).vault
    cloned = clone_vault(tmp_path / "pc2", REPO, host).vault

    for vault in (created, cloned):
        runner = GitRunner(vault.path, IDENTITY)
        assert configured_helpers(runner, GITHUB_URL) == ["", helper.command]
        assert "password=helper-answer" in credential_fill(vault.path)


def test_setup_again_repairs_a_vault_set_up_without_the_helper(
    tmp_path: Path, helper: CredentialHelper
) -> None:
    create_vault(tmp_path / "pc1", REPO, "Ana", LocalHost(tmp_path / "github"))
    runner = GitRunner(tmp_path / "pc1", IDENTITY)
    assert configured_helpers(runner, GITHUB_URL) == []

    result = create_vault(
        tmp_path / "pc1", REPO, "Ana", LocalHost(tmp_path / "github", helper=helper)
    )

    assert result.action == "already-set-up"
    assert configured_helpers(runner, GITHUB_URL) == ["", helper.command]


def test_ensure_is_none_for_a_host_without_a_helper(tmp_path: Path) -> None:
    host = LocalHost(tmp_path / "github")
    vault = create_vault(tmp_path / "pc1", REPO, "Ana", host).vault

    assert ensure_credential_helper(vault.path, host) is None
    assert not (vault.path / ".git" / "config").read_text().count("credential")


def test_the_unattended_probe_reports_an_unreachable_remote(tmp_path: Path) -> None:
    host = LocalHost(tmp_path / "github")
    vault = create_vault(tmp_path / "pc1", REPO, "Ana", host).vault
    runner = unattended_runner(vault.path, IDENTITY, timeout=30)

    assert probe_unattended_access(runner).ok
    git(vault.path, "remote", "set-url", "origin", f"{remote_base(tmp_path)}/missing.git")
    result = probe_unattended_access(runner)
    assert not result.ok
    assert result.describe().startswith("git ls-remote exited")
