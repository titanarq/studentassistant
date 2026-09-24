"""The CLI: `version` prints `__version__`, `serve` hands uvicorn the configured host and port."""

from __future__ import annotations

import importlib
import os
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

from studentassistant import __version__
from studentassistant.cli import cli

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture
def runner() -> CliRunner:
    """Click's runner over the Typer app: it invokes a command in-process, with no subprocess."""
    return CliRunner()


@pytest.fixture
def config_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `SA_CONFIG` at a file under `tmp_path` and drop every `SA_*` the environment carries.

    No test may read `~/.config/studentassistant` (AGENTS.md), and `serve` loads the real
    configuration, so the machine's own file has to be taken out of the picture.
    """
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    return path


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace `uvicorn.run` with a recorder, so `serve` never binds a port or starts a server."""
    calls: list[dict[str, Any]] = []

    def record(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", record)
    return calls


def write_toml(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")


def test_version_prints_the_package_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["version"])

    assert result.exception is None
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_serve_hands_uvicorn_the_configured_host_and_port(
    runner: CliRunner,
    config_toml: Path,
    uvicorn_run: list[dict[str, Any]],
) -> None:
    write_toml(
        config_toml,
        """
        [server]
        host = "127.0.0.1"
        port = 9100
        """,
    )

    result = runner.invoke(cli, ["serve"])

    assert result.exception is None
    assert result.exit_code == 0
    assert len(uvicorn_run) == 1
    assert uvicorn_run[0]["host"] == "127.0.0.1"
    assert uvicorn_run[0]["port"] == 9100


def test_serve_takes_the_host_and_port_from_the_environment(
    runner: CliRunner,
    config_toml: Path,
    uvicorn_run: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_toml(
        config_toml,
        """
        [server]
        host = "127.0.0.1"
        port = 9100
        """,
    )
    monkeypatch.setenv("SA_SERVER__PORT", "9999")

    result = runner.invoke(cli, ["serve"])

    assert result.exit_code == 0
    # The environment still wins over the file, because the CLI reads the configuration whole.
    assert uvicorn_run[0]["port"] == 9999
    assert uvicorn_run[0]["host"] == "127.0.0.1"


def test_serve_passes_uvicorn_an_app_built_by_the_factory(
    runner: CliRunner,
    config_toml: Path,
    uvicorn_run: list[dict[str, Any]],
) -> None:
    result = runner.invoke(cli, ["serve"])

    assert result.exit_code == 0
    served_app = uvicorn_run[0]["app"]
    assert isinstance(served_app, FastAPI)
    assert served_app.version == __version__


def test_help_offers_both_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output
    assert "version" in result.output


def test_the_console_script_names_a_callable_target() -> None:
    """`pip`'s entry point is a string: a typo in it would survive every other test here."""
    scripts = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]["scripts"]

    module_name, separator, attribute = scripts["studentassistant"].partition(":")

    assert separator == ":"
    assert callable(getattr(importlib.import_module(module_name), attribute))
