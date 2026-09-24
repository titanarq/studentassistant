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


# --- setup -------------------------------------------------------------------------------------


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A local "GitHub" of bare repositories, handed to `setup` in place of `select_host()`."""
    import studentassistant.cli as cli_module
    from github_fakes import LocalHost

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    host = LocalHost(tmp_path / "github")
    monkeypatch.setattr(cli_module, "_github_host", lambda: host)
    return host


def test_setup_create_non_interactive_writes_the_config(
    runner: CliRunner, config_toml: Path, github: Any, tmp_path: Path
) -> None:
    vault_path = tmp_path / "vault"
    args = ["setup", "--vault-repo", "ana/vault", "--path", str(vault_path), "--create"]

    result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert "Vault creado" in result.output
    assert github.created == ["ana/vault"]
    stored = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    assert stored["vault"] == {"path": str(vault_path), "repo": "ana/vault"}


def test_setup_rerun_with_the_same_answers_is_a_no_op(
    runner: CliRunner, config_toml: Path, github: Any, tmp_path: Path
) -> None:
    args = ["setup", "--vault-repo", "ana/vault", "--path", str(tmp_path / "vault"), "--create"]
    assert runner.invoke(cli, args).exit_code == 0
    before = config_toml.read_bytes()

    again = runner.invoke(cli, args)

    assert again.exit_code == 0, again.output
    assert "no hay nada que hacer" in again.output
    assert config_toml.read_bytes() == before
    assert github.created == ["ana/vault"]


def test_setup_clone_interactive_asks_in_spanish(
    runner: CliRunner, config_toml: Path, github: Any, tmp_path: Path
) -> None:
    from studentassistant.vault.setup import create_vault

    create_vault(tmp_path / "pc1", "ana/vault", "Ana", github)
    write_toml(config_toml, '[server]\nport = 9100\n\n[vault]\npath = "%s"\n' % (tmp_path / "pc2"))

    result = runner.invoke(cli, ["setup"], input="clonar\nana/vault\n\n")

    assert result.exit_code == 0, result.output
    assert "¿Quieres crear un vault nuevo o clonar uno" in result.output
    assert "Repositorio de GitHub (propietario/nombre)" in result.output
    assert f"Carpeta local del vault [{tmp_path / 'pc2'}]" in result.output
    assert "Vault clonado de ana/vault" in result.output
    stored = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    assert stored["server"] == {"port": 9100}
    assert stored["vault"] == {"path": str(tmp_path / "pc2"), "repo": "ana/vault"}


def test_setup_create_interactive_asks_the_name_and_reasks_a_bad_repo(
    runner: CliRunner, config_toml: Path, github: Any, tmp_path: Path
) -> None:
    from studentassistant.vault import Vault

    vault_path = tmp_path / "vault"
    answers = f"crear\nmal\nana/vault\n{vault_path}\nAna García\n"

    result = runner.invoke(cli, ["setup"], input=answers)

    assert result.exit_code == 0, result.output
    assert "«mal» no es válido" in result.output
    assert "Tu nombre" in result.output
    assert Vault.open(vault_path).meta.student == "Ana García"


def test_setup_failure_is_reported_in_spanish_and_writes_no_config(
    runner: CliRunner, config_toml: Path, github: Any, tmp_path: Path
) -> None:
    args = ["setup", "--vault-repo", "ana/vault", "--path", str(tmp_path / "pc2"), "--clone"]

    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    assert "No se pudo preparar el vault" in result.output
    assert "no existe en GitHub" in result.output
    assert not config_toml.exists()


def test_setup_refuses_both_modes(runner: CliRunner, config_toml: Path, github: Any) -> None:
    result = runner.invoke(cli, ["setup", "--create", "--clone"])
    assert result.exit_code == 2
    assert "Elige solo una opción" in result.output


def test_setup_with_a_token_never_writes_or_prints_it(
    runner: CliRunner,
    config_toml: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Built from pieces, like `tests/vault/secret_samples.py`, so no scanner flags the source.
    token = "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"

    import studentassistant.cli as cli_module
    from github_fakes import bare_repo, remote_base
    from studentassistant.vault.github import TokenHost

    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "github"
    bare_repo(root, "ana/vault")
    host = TokenHost(token, remote_base=remote_base(root))
    monkeypatch.setattr(cli_module, "_github_host", lambda: host)
    vault_path = tmp_path / "vault"

    result = runner.invoke(
        cli, ["setup", "--vault-repo", "ana/vault", "--path", str(vault_path), "--create"]
    )

    assert result.exit_code == 0, result.output
    assert "Aviso: sin `gh` no se puede comprobar que el repositorio ana/vault sea privado" in (
        result.output
    )
    assert token not in result.output
    assert token not in config_toml.read_text(encoding="utf-8")
    assert token not in (vault_path / ".git" / "config").read_text(encoding="utf-8")
