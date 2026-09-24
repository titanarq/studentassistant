"""The `stt` configuration section: defaults, TOML, `SA_*` on top, mode validation."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from studentassistant.config import Settings


@pytest.fixture
def config_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`SA_CONFIG` at a file under `tmp_path`, with every `SA_*` of the environment dropped."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    return path


def write_toml(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")


def test_stt_defaults(config_toml: Path) -> None:
    stt = Settings().stt

    assert stt.mode == "client"
    assert stt.provider == "web-speech"
    assert stt.language == "es"
    assert stt.options == {}
    assert stt.provider_options() == {}


def test_toml_overrides_mode_provider_and_options(config_toml: Path) -> None:
    write_toml(
        config_toml,
        """
        [stt]
        mode = "server"
        provider = "faster-whisper"

        [stt.options.faster-whisper]
        model = "large-v3"
        beam_size = 5
        """,
    )

    stt = Settings().stt

    assert stt.mode == "server"
    assert stt.provider == "faster-whisper"
    assert stt.language == "es"
    assert stt.options["faster-whisper"]["model"] == "large-v3"
    assert stt.provider_options() == {"model": "large-v3", "beam_size": 5}
    assert stt.provider_options("web-speech") == {}


def test_env_var_overrides_the_toml(config_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_toml(
        config_toml,
        """
        [stt]
        mode = "server"
        provider = "faster-whisper"
        """,
    )
    monkeypatch.setenv("SA_STT__PROVIDER", "fake")
    monkeypatch.setenv("SA_STT__LANGUAGE", "ca")

    stt = Settings().stt

    assert stt.provider == "fake"
    assert stt.language == "ca"
    assert stt.mode == "server"


def test_unknown_mode_is_rejected(config_toml: Path) -> None:
    write_toml(config_toml, '[stt]\nmode = "hybrid"\n')

    with pytest.raises(ValidationError):
        Settings()
