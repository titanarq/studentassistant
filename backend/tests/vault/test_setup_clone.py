"""`clone_vault`: a vault brought to a new PC, checked, then handed to the post-clone hook."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from git_helpers import git

from github_fakes import LocalHost, bare_repo
from studentassistant.vault import Vault
from studentassistant.vault.setup import SetupError, clone_vault, create_vault

REPO = "ana/vault"
STUDENT = "Ana García"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def host(tmp_path: Path) -> LocalHost:
    return LocalHost(tmp_path / "github")


@pytest.fixture
def published(tmp_path: Path, host: LocalHost) -> Vault:
    """The vault the student created on the first PC."""
    return create_vault(tmp_path / "pc1", REPO, STUDENT, host).vault


def test_clone_brings_the_vault_and_calls_the_hook_once(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    calls: list[Vault] = []

    result = clone_vault(tmp_path / "pc2" / "vault", REPO, host, post_clone=calls.append)

    assert result.action == "cloned"
    assert result.vault.meta == published.meta
    assert calls == [result.vault]
    assert git(result.vault.path, "remote", "get-url", "origin").strip() == host.remote_url(REPO)


def test_clone_refuses_a_non_empty_directory(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    target = tmp_path / "pc2"
    target.mkdir()
    (target / "nota.txt").write_text("x")
    calls: list[Vault] = []

    with pytest.raises(SetupError, match="no está vacío"):
        clone_vault(target, REPO, host, post_clone=calls.append)
    assert calls == []
    assert [p.name for p in target.iterdir()] == ["nota.txt"]


def test_clone_into_an_empty_directory(tmp_path: Path, host: LocalHost, published: Vault) -> None:
    target = tmp_path / "pc2"
    target.mkdir()
    assert clone_vault(target, REPO, host).action == "cloned"


def test_clone_of_a_missing_repository_is_refused(tmp_path: Path, host: LocalHost) -> None:
    with pytest.raises(SetupError, match="no existe en GitHub"):
        clone_vault(tmp_path / "pc2", REPO, host)
    assert not (tmp_path / "pc2").exists()


def test_clone_of_a_future_format_is_refused_and_left_in_place(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    meta_path = published.path / "vault.yaml"
    meta = yaml.safe_load(meta_path.read_text())
    meta["format_version"] = 99
    meta_path.write_text(yaml.safe_dump(meta))
    git(published.path, "commit", "-am", "formato del futuro")
    git(published.path, "push", "--quiet", "origin", "main")
    calls: list[Vault] = []

    with pytest.raises(SetupError, match="versión de formato") as error:
        clone_vault(tmp_path / "pc2", REPO, host, post_clone=calls.append)

    assert "actualiza Student Assistant" in str(error.value)
    assert calls == []
    assert (tmp_path / "pc2" / "vault.yaml").is_file()


def test_clone_of_a_repository_that_is_not_a_vault_is_refused(
    tmp_path: Path, host: LocalHost
) -> None:
    bare_repo(host.root, REPO)

    with pytest.raises(SetupError, match="no contiene un vault"):
        clone_vault(tmp_path / "pc2", REPO, host)


def test_clone_without_push_access_is_reported_after_cloning(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    class ReadOnlyHost(LocalHost):
        """Fetches work, pushes go to a remote that does not exist: a read-only credential."""

        def git_environment(self) -> dict[str, str]:
            return {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "remote.origin.pushurl",
                "GIT_CONFIG_VALUE_0": str(tmp_path / "sin-permiso.git"),
            }

    calls: list[Vault] = []

    with pytest.raises(SetupError, match="no hay permiso para subir cambios"):
        clone_vault(tmp_path / "pc2", REPO, ReadOnlyHost(host.root), post_clone=calls.append)
    assert calls == []
    assert (tmp_path / "pc2" / "vault.yaml").is_file()


def test_clone_twice_with_the_same_answers_changes_nothing(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    calls: list[Vault] = []
    first = clone_vault(tmp_path / "pc2", REPO, host, post_clone=calls.append)
    head = git(first.vault.path, "rev-parse", "HEAD")

    again = clone_vault(tmp_path / "pc2", REPO, host, post_clone=calls.append)

    assert again.action == "already-set-up"
    assert len(calls) == 1
    assert git(again.vault.path, "rev-parse", "HEAD") == head
    assert git(again.vault.path, "status", "--porcelain") == ""


def test_clone_over_a_vault_of_another_repository_is_refused(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    create_vault(tmp_path / "otro", "ana/otro", STUDENT, host)

    with pytest.raises(SetupError, match="no es un vault de este repositorio"):
        clone_vault(tmp_path / "otro", REPO, host)
