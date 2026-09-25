"""`check_remote_access`: the vault's origin, whether it is the configured repo, may we push."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from github_fakes import LocalHost
from studentassistant.vault import Vault
from studentassistant.vault.setup import SetupError, check_remote_access, create_vault


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def host(tmp_path: Path) -> LocalHost:
    return LocalHost(tmp_path / "github")


def test_a_set_up_vault_may_push(tmp_path: Path, host: LocalHost) -> None:
    create_vault(tmp_path / "vault", "ana/vault", "Ana", host)

    access = check_remote_access(tmp_path / "vault", host, "ana/vault")

    assert access.url == host.remote_url("ana/vault")
    assert access.repo_matches is True
    assert access.push_error is None


def test_another_repo_and_no_host(tmp_path: Path, host: LocalHost) -> None:
    create_vault(tmp_path / "vault", "ana/vault", "Ana", host)

    assert check_remote_access(tmp_path / "vault", host, "ana/other").repo_matches is False
    assert check_remote_access(tmp_path / "vault", None, "ana/vault").repo_matches is None


def test_an_unreachable_origin_is_a_push_error(tmp_path: Path, host: LocalHost) -> None:
    create_vault(tmp_path / "vault", "ana/vault", "Ana", host)
    shutil.rmtree(tmp_path / "github")

    access = check_remote_access(tmp_path / "vault", host, "ana/vault")

    assert access.push_error is not None
    assert "no hay permiso para subir cambios" in access.push_error


def test_no_repository_or_no_origin_raises(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="no es un repositorio git"):
        check_remote_access(tmp_path / "nothing", None)
    Vault.init(tmp_path / "vault", student="Ana")
    with pytest.raises(SetupError, match="no tiene `origin`"):
        check_remote_access(tmp_path / "vault", None)
