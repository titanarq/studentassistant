"""Configuration: the documented defaults, the TOML file named by `SA_CONFIG`, `SA_*` on top."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from studentassistant.config import Settings, config_toml_path


@pytest.fixture
def config_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `SA_CONFIG` at a file under `tmp_path` and drop every `SA_*` the environment carries.

    No test may read `~/.config/studentassistant` (AGENTS.md), so the machine's own configuration is
    taken out of the picture even for the tests that never write the file below.
    """
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    return path


def write_toml(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")


def test_defaults_when_no_toml_file_exists(config_toml: Path) -> None:
    assert not config_toml.exists()

    settings = Settings()

    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 8765
    assert settings.vault.path == Path.home() / "StudentAssistant" / "vault"
    assert settings.llm.roles.observer.model == "claude-sonnet-5"
    assert settings.llm.roles.transcriber.model == "claude-sonnet-5"
    assert settings.llm.roles.editor.model == "claude-opus-5-5"
    assert settings.llm.roles.generator.model == "claude-opus-5-5"


def test_toml_file_overrides_the_defaults(config_toml: Path) -> None:
    write_toml(
        config_toml,
        """
        [server]
        host = "127.0.0.1"
        port = 9100

        [vault]
        path = "~/otro-vault"

        [llm.roles.editor]
        model = "claude-opus-del-toml"
        """,
    )

    settings = Settings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9100
    assert settings.vault.path == Path.home() / "otro-vault"
    assert settings.llm.roles.editor.model == "claude-opus-del-toml"
    # What the file does not mention keeps its default.
    assert settings.llm.roles.observer.model == "claude-sonnet-5"
    assert settings.llm.roles.generator.model == "claude-opus-5-5"


def test_env_var_overrides_the_toml_file(
    config_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(
        config_toml,
        """
        [server]
        host = "127.0.0.1"
        port = 9100

        [llm.roles.editor]
        model = "claude-opus-del-toml"
        """,
    )
    monkeypatch.setenv("SA_SERVER__PORT", "9999")
    monkeypatch.setenv("SA_LLM__ROLES__EDITOR__MODEL", "claude-opus-del-entorno")

    settings = Settings()

    assert settings.server.port == 9999
    assert settings.llm.roles.editor.model == "claude-opus-del-entorno"
    # The environment only replaces what it names: the file's host still applies.
    assert settings.server.host == "127.0.0.1"


def test_config_toml_path_defaults_to_the_documented_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SA_CONFIG", raising=False)

    assert config_toml_path() == Path.home() / ".config" / "studentassistant" / "config.toml"


def test_config_toml_path_expands_sa_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_CONFIG", "~/otra-configuracion/config.toml")

    assert config_toml_path() == Path.home() / "otra-configuracion" / "config.toml"


def test_write_vault_config_creates_the_file_with_path_and_repo(
    config_toml: Path, tmp_path: Path
) -> None:
    from studentassistant.config import write_vault_config

    assert write_vault_config(tmp_path / "vault", "ana/vault") is True

    settings = Settings()
    assert settings.vault.path == tmp_path / "vault"
    assert settings.vault.repo == "ana/vault"


def test_write_vault_config_keeps_every_other_key_and_comment(
    config_toml: Path, tmp_path: Path
) -> None:
    from studentassistant.config import write_vault_config

    write_toml(
        config_toml,
        """
        # mi configuración
        [server]
        port = 9100  # otro puerto

        [vault]
        path = "~/viejo"

        [vault.git]
        remote = "upstream"
        """,
    )

    write_vault_config(tmp_path / "vault", "ana/vault")

    text = config_toml.read_text(encoding="utf-8")
    assert "# mi configuración" in text
    assert "port = 9100  # otro puerto" in text
    settings = Settings()
    assert settings.server.port == 9100
    assert settings.vault.git.remote == "upstream"
    assert settings.vault.path == tmp_path / "vault"
    assert settings.vault.repo == "ana/vault"


def test_write_vault_config_again_with_the_same_values_changes_nothing(
    config_toml: Path, tmp_path: Path
) -> None:
    from studentassistant.config import write_vault_config

    write_vault_config(tmp_path / "vault", "ana/vault")
    before = config_toml.read_bytes()
    mtime = config_toml.stat().st_mtime_ns

    assert write_vault_config(tmp_path / "vault", "ana/vault") is False
    assert config_toml.read_bytes() == before
    assert config_toml.stat().st_mtime_ns == mtime


def test_write_vault_config_refuses_a_malformed_repo(config_toml: Path, tmp_path: Path) -> None:
    from studentassistant.config import write_vault_config

    with pytest.raises(ValueError):
        write_vault_config(tmp_path / "vault", "no-es-un-repo")
    assert not config_toml.exists()


def test_vault_repo_is_optional_and_validated(config_toml: Path) -> None:
    assert Settings().vault.repo is None
    write_toml(config_toml, '[vault]\nrepo = "mal repo"\n')
    with pytest.raises(ValueError):
        Settings()


def test_write_vault_config_keeps_the_mode_of_an_existing_file(
    config_toml: Path, tmp_path: Path
) -> None:
    from studentassistant.config import write_vault_config

    write_toml(config_toml, "[server]\nport = 9100\n")
    config_toml.chmod(0o600)

    assert write_vault_config(tmp_path / "vault", "ana/vault") is True

    assert config_toml.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in config_toml.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_write_vault_config_creates_a_new_file_owner_only(
    config_toml: Path, tmp_path: Path
) -> None:
    from studentassistant.config import write_vault_config

    old_umask = os.umask(0o002)
    try:
        write_vault_config(tmp_path / "vault", "ana/vault")
    finally:
        os.umask(old_umask)

    assert config_toml.stat().st_mode & 0o777 == 0o600


def test_the_digest_timezone_defaults_to_the_local_zone_and_refuses_an_unknown_name(
    config_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError

    monkeypatch.setenv("TZ", "America/Bogota")
    assert Settings().observer.digest_timezone is None
    assert Settings().observer.digest_zone() == ZoneInfo("America/Bogota")

    write_toml(config_toml, '[observer]\ndigest_timezone = "Europe/Madrid"\n')
    assert Settings().observer.digest_zone() == ZoneInfo("Europe/Madrid")

    monkeypatch.setenv("SA_OBSERVER__DIGEST_TIMEZONE", "Europe/Atlantida")
    with pytest.raises(ValidationError, match="unknown timezone 'Europe/Atlantida'"):
        Settings()
