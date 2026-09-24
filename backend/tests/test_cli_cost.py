"""`studentassistant cost [--topic]`: per-topic USD and tokens from the vault's ledgers."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.vault import (
    LedgerEntry,
    Vault,
    append_ledger_entry,
    create_subject,
    create_topic,
)


def entry(subject: str, topic: str, usd: float | None, **overrides: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "time": datetime.now(UTC),
        "role": "editor",
        "model": "claude-opus-5-5",
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_tokens": 50,
        "cache_write_tokens": 5,
        "estimated_usd": usd,
        "subject": subject,
        "topic": topic,
    }
    fields.update(overrides)
    return LedgerEntry.model_validate(fields)


@pytest.fixture
def vault(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """`tmp_vault` as the configured vault: two topics with spend, one without."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    maths = create_subject(tmp_vault, "Matemáticas").slug
    derivadas = create_topic(tmp_vault, maths, "Derivadas").slug
    integrales = create_topic(tmp_vault, maths, "Integrales").slug
    create_topic(tmp_vault, maths, "Límites")
    append_ledger_entry(tmp_vault, maths, derivadas, entry(maths, derivadas, 0.25))
    append_ledger_entry(tmp_vault, maths, derivadas, entry(maths, derivadas, None))
    old = datetime.now(UTC) - timedelta(days=2)
    append_ledger_entry(tmp_vault, maths, integrales, entry(maths, integrales, 1.5, time=old))
    return tmp_vault


def test_totals_per_topic_and_today(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["cost"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        "matematicas/derivadas: $0.2500 (entrada 2000, salida 200, caché leída 100, "
        "caché escrita 10 tokens; 2 llamadas, 1 sin precio conocido)"
    )
    assert lines[1].startswith("matematicas/integrales: $1.5000 (entrada 1000,")
    assert lines[2] == "Hoy (UTC): $0.2500"
    assert "limites" not in result.output


def test_one_topic_only(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["cost", "--topic", "matematicas/integrales"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("matematicas/integrales: $1.5000")
    assert "derivadas" not in result.output
    assert result.output.splitlines()[-1] == "Hoy (UTC): $0.2500"


def test_a_topic_without_spend_prints_zero(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["cost", "--topic", "matematicas/limites"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("matematicas/limites: $0.0000 (")


@pytest.mark.parametrize("topic", ["matematicas/no-existe", "fisica/derivadas"])
def test_unknown_topic_fails_in_spanish(vault: Vault, topic: str) -> None:
    result = CliRunner().invoke(cli, ["cost", "--topic", topic])

    assert result.exit_code == 1
    assert result.output.strip() == f"No existe el tema «{topic}» en la bóveda."


def test_malformed_topic_fails(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["cost", "--topic", "derivadas"])

    assert result.exit_code == 1
    assert "--topic <asignatura>/<tema>" in result.output


def test_an_empty_vault_says_so(tmp_vault: Vault, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))

    result = CliRunner().invoke(cli, ["cost"])

    assert result.exit_code == 0
    assert result.output.splitlines() == ["Sin gasto registrado.", "Hoy (UTC): $0.0000"]


def test_a_missing_vault_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "no-vault"))

    result = CliRunner().invoke(cli, ["cost"])

    assert result.exit_code == 1
    assert result.output.startswith("No se puede abrir la bóveda:")
