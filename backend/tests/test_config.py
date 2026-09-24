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
