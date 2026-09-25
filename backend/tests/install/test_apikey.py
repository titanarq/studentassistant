"""The API key file: stored with mode 600, other lines kept, exported only when the env has none."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from studentassistant.config import Settings
from studentassistant.install.apikey import (
    API_KEY_ENV_VAR,
    export_api_key,
    file_is_private,
    read_api_key,
    read_env_file,
    store_api_key,
)

# Built from pieces so no secret scanner flags the source.
KEY = "sk-" + "ant-" + "api03-" + "x" * 40


def test_store_creates_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "secrets.env"

    assert store_api_key(path, KEY) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert file_is_private(path)
    assert path.read_text(encoding="utf-8") == f"{API_KEY_ENV_VAR}={KEY}\n"
    assert read_api_key(path) == KEY


def test_store_is_idempotent_and_keeps_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("# mine\nOTHER=1\nANTHROPIC_API_KEY=old\n", encoding="utf-8")
    path.chmod(0o644)

    assert store_api_key(path, KEY) is True
    assert path.read_text(encoding="utf-8") == f"# mine\nOTHER=1\n{API_KEY_ENV_VAR}={KEY}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.stat().st_mtime_ns

    assert store_api_key(path, KEY) is False
    assert path.stat().st_mtime_ns == before


def test_store_refuses_a_key_with_spaces(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        store_api_key(tmp_path / "secrets.env", "two words")
    assert not (tmp_path / "secrets.env").exists()


def test_read_env_file_skips_comments_and_strips_quotes(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("\n# c\nA=\"1\"\nB='2'\nnot a pair\n", encoding="utf-8")

    assert read_env_file(path) == {"A": "1", "B": "2"}
    assert read_env_file(tmp_path / "absent") == {}
    assert read_api_key(path) is None


def test_export_only_when_the_environment_has_no_key(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    store_api_key(path, KEY)

    environ: dict[str, str] = {}
    assert export_api_key(path, environ) is True
    assert environ == {API_KEY_ENV_VAR: KEY}

    preset = {API_KEY_ENV_VAR: "from-env"}
    assert export_api_key(path, preset) is False
    assert preset == {API_KEY_ENV_VAR: "from-env"}

    assert export_api_key(tmp_path / "absent", {}) is False


def test_the_key_file_defaults_next_to_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "conf" / "config.toml"))
    monkeypatch.delenv("SA_LLM__API_KEY_FILE", raising=False)

    assert Settings().llm.api_key_path() == tmp_path / "conf" / "secrets.env"

    monkeypatch.setenv("SA_LLM__API_KEY_FILE", str(tmp_path / "elsewhere.env"))
    assert Settings().llm.api_key_path() == tmp_path / "elsewhere.env"
