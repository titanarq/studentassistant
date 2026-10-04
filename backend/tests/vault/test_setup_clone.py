"""`clone_vault`: a vault brought to a new PC, checked, then handed to the post-clone hook."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from git_helpers import git

from github_fakes import LocalHost, bare_repo
from studentassistant.vault import Vault
from studentassistant.vault.models import LEGACY_FORMAT_VERSION
from studentassistant.vault.setup import SetupError, clone_vault, create_vault
from studentassistant.vault.vault import MIGRATE_USERS_COMMAND

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


def _publish_as_format_one(vault: Vault) -> None:
    """Leave the published repository the way an installation of format 1 left it.

    That layout held the content at the root and had no `users/` at all, and its `vault.yaml`
    declared the three fields it had: no `legacy_root_user`, which is what format 2 writes to say
    who received the root's content (#548).
    """
    meta_path = vault.path / "vault.yaml"
    published = yaml.safe_load(meta_path.read_text())
    meta_path.write_text(
        yaml.safe_dump(
            {
                "format_version": LEGACY_FORMAT_VERSION,
                "created_at": published["created_at"],
                "student": published["student"],
            }
        )
    )
    git(vault.path, "rm", "-r", "--quiet", "users")
    git(vault.path, "commit", "-qam", "un vault de formato 1")
    git(vault.path, "push", "--quiet", "origin", "main")


def test_clone_of_a_format_one_vault_succeeds_and_says_how_to_migrate_it(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    """A student who brings their notes to a new PC ends the flow with them on disk and the one
    command that unlocks them: refusing the clone would leave a directory they may not use and no
    way to find out why (#548)."""
    _publish_as_format_one(published)
    warnings: list[str] = []
    calls: list[Vault] = []

    result = clone_vault(
        tmp_path / "pc2", REPO, host, post_clone=calls.append, warn=warnings.append
    )

    assert result.action == "cloned"
    assert result.vault.meta.format_version == LEGACY_FORMAT_VERSION
    assert result.vault.root == result.vault.path and result.vault.user_id is None, (
        "what it hands back is the migration's own handle: the content to move is the root's"
    )
    assert calls == [result.vault], "the index rebuild still runs, over that same handle"
    assert len(warnings) == 1
    assert warnings[0].startswith("Aviso: ")
    assert MIGRATE_USERS_COMMAND in warnings[0]
    assert "formato 1" in warnings[0]


def test_a_format_one_vault_that_is_already_on_this_pc_warns_the_same_way(
    tmp_path: Path, host: LocalHost, published: Vault
) -> None:
    """Re-running `setup` with the same answers is idempotent, but the vault still needs migrating,
    so the warning is not something only the first run says. The first clone here is given no
    `warn` at all, which is the default: a caller that does not ask for warnings still succeeds."""
    _publish_as_format_one(published)
    clone_vault(tmp_path / "pc2", REPO, host)
    warnings: list[str] = []

    again = clone_vault(tmp_path / "pc2", REPO, host, warn=warnings.append)

    assert again.action == "already-set-up"
    assert again.vault.meta.format_version == LEGACY_FORMAT_VERSION
    assert len(warnings) == 1
    assert warnings[0].startswith("Aviso: ")
    assert MIGRATE_USERS_COMMAND in warnings[0]


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
