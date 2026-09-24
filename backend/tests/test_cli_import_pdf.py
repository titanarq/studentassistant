"""`studentassistant import-pdf <subject>/<topic> <file> [--pages]`: a PDF source, committed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.vault import Vault, create_subject, create_topic, list_sources


@pytest.fixture
def vault(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """`tmp_vault` as the configured vault, with the topic `historia/revolucion-industrial`."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    subject = create_subject(tmp_vault, "Historia").slug
    create_topic(tmp_vault, subject, "Revolución industrial")
    return tmp_vault


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    document = pymupdf.open()
    for number in range(1, 13):
        page = document.new_page()
        if number != 11:
            page.insert_text((72, 72), f"Página {number}")
    path = tmp_path / "Tema 4.pdf"
    path.write_bytes(document.tobytes())
    return path


def _head_subject(vault: Vault) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=vault.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_page_range_is_imported_and_committed(vault: Vault, pdf: Path) -> None:
    result = CliRunner().invoke(
        cli, ["import-pdf", "historia/revolucion-industrial", str(pdf), "--pages", "9-11"]
    )

    assert result.exit_code == 0, result.output
    assert "PDF importado como sources/pdf/page-001.pdf: páginas 9-11" in result.output
    assert "(3 páginas)" in result.output
    assert "Sin texto extraíble (¿escaneadas?): páginas 11." in result.output
    [source] = list_sources(vault, "historia", "revolucion-industrial")
    assert source.meta is not None
    assert source.meta["first_page"] == 9
    assert _head_subject(vault) == "Importar PDF Tema 4.pdf en historia/revolucion-industrial"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=vault.path, capture_output=True, text=True
    )
    assert status.stdout == ""


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["historia", "{pdf}"], 2, "no es un tema"),
        (["historia/no-existe", "{pdf}"], 1, "No existe el tema"),
        (["historia/revolucion-industrial", "{missing}"], 1, "No existe el archivo"),
        (["historia/revolucion-industrial", "{pdf}", "--pages", "ochenta"], 1, "82-94"),
        (["historia/revolucion-industrial", "{pdf}", "--pages", "10-20"], 1, "tiene 12 páginas"),
    ],
)
def test_refusals_explain_themselves_and_store_nothing(
    vault: Vault, pdf: Path, args: list[str], code: int, message: str
) -> None:
    argv = [a.format(pdf=pdf, missing=pdf.with_name("otro.pdf")) for a in args]

    result = CliRunner().invoke(cli, ["import-pdf", *argv])

    assert result.exit_code == code, result.output
    assert message in result.output
    assert list_sources(vault, "historia", "revolucion-industrial") == []


def test_a_pdf_over_the_configured_size_is_refused_before_reading(
    vault: Vault, pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_SOURCES__MAX_PDF_BYTES", "100")

    result = CliRunner().invoke(cli, ["import-pdf", "historia/revolucion-industrial", str(pdf)])

    assert result.exit_code == 1
    assert "supera el máximo que se importa" in result.output
