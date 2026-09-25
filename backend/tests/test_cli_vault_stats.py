"""`studentassistant vault stats [--top N] [--json]`: the size as a Spanish table or JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.vault import Vault

PAGE = "subjects/mates/topics/derivadas/sources/notes/page-001.jpg"
NOTES = "subjects/mates/topics/derivadas/notes/apuntes.md"


@pytest.fixture
def vault(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    for relative, size in ((PAGE, 4096), (NOTES, 512)):
        path = tmp_vault.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return tmp_vault


def test_table_shows_categories_topics_and_largest_files(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["vault", "stats", "--top", "1"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Tamaño del vault: ")
    assert "Por categoría:" in result.output
    assert "imágenes de fuentes" in result.output and "4.0 KB" in result.output
    assert "  mates: " in result.output and "    derivadas: " in result.output
    assert "Los 1 ficheros más grandes:" in result.output
    assert PAGE in result.output and NOTES not in result.output.split("más grandes:")[1]


def test_json_is_machine_readable(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["vault", "stats", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    categories = {entry["category"]: entry["bytes"] for entry in data["categories"]}
    assert categories["source_images"] == 4096 and categories["notes"] == 512
    assert data["largest_files"][0]["path"] == PAGE
    assert data["total_bytes"] == data["working_tree_bytes"] + data["git_bytes"]


def test_a_missing_vault_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "nope"))

    result = CliRunner().invoke(cli, ["vault", "stats"])

    assert result.exit_code == 1
    assert "No se puede abrir la bóveda" in result.output
