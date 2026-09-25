"""`studentassistant eval run`: the estimate first, a confirmation, then the run and its report."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import studentassistant.evals.cli as eval_cli_module
from eval_fixtures import PipelineClaude, make_case
from studentassistant.cli import cli


@pytest.fixture
def evals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("SA_LLM__API_KEY_FILE", str(tmp_path / "secrets.env"))
    path = tmp_path / "evals"
    monkeypatch.setenv("SA_EVAL__PATH", str(path))
    monkeypatch.setenv("SA_EVAL__SPEED", "1000")
    make_case(path)
    return path


def test_declining_the_estimate_calls_nobody(evals: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_transport() -> None:
        raise AssertionError("no transport may be built before the cost is confirmed")

    monkeypatch.setattr(eval_cli_module, "_eval_transport", no_transport)
    result = CliRunner().invoke(cli, ["eval", "run"], input="n\n")
    assert result.exit_code == 1, result.output
    assert "Coste estimado" in result.output and "Total estimado:" in result.output
    assert "observer (claude-sonnet-5)" in result.output
    assert "Cancelado" in result.output
    assert not (evals / "runs").exists()


def test_a_confirmed_run_writes_the_report(evals: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = PipelineClaude(evals / "runs")
    monkeypatch.setattr(eval_cli_module, "_eval_transport", lambda: claude)
    result = CliRunner().invoke(cli, ["eval", "run", "--yes", "--case", "celula"])
    assert result.exit_code == 0, result.output
    [run] = (evals / "runs").iterdir()
    assert (run / "report.md").is_file() and (run / "report.json").is_file()
    assert (run / "celula" / "vault" / "vault.yaml").is_file()
    assert f"Informe: {run / 'report.md'}" in result.output
    assert "celula: global" in result.output
    assert not (Path(os.environ["HOME"]) / "vault").exists()


def test_an_eval_set_inside_the_vault_is_refused(
    evals: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_EVAL__PATH", str(tmp_path / "vault" / "evals"))
    result = CliRunner().invoke(cli, ["eval", "run", "--yes"])
    assert result.exit_code == 1 and "bóveda" in result.output


def test_an_empty_or_broken_eval_set_is_reported(
    evals: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("SA_EVAL__PATH", str(empty))
    result = CliRunner().invoke(cli, ["eval", "run", "--yes"])
    assert result.exit_code == 1 and "No hay casos" in result.output
    (evals / "celula" / "reference" / "notes.md").unlink()
    monkeypatch.setenv("SA_EVAL__PATH", str(evals))
    result = CliRunner().invoke(cli, ["eval", "run", "--yes"])
    assert result.exit_code == 1 and "No se puede leer" in result.output
