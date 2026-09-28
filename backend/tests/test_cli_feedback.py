"""`studentassistant feedback list|mark`: the triage of the app feedback inbox (#472)."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.vault import (
    FeedbackContext,
    Vault,
    add_feedback,
    feedback_path,
    get_feedback,
    list_feedback,
    read_jsonl,
)
from studentassistant.vault.feedback import FeedbackLine
from studentassistant.vault.locking import vault_lock

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def vault(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    add_feedback(
        tmp_vault,
        "bug",
        "El micro se corta",
        "Al hablar mucho rato el micro se corta.",
        FeedbackContext(subject="mates", topic="derivadas", route="study", mode="estudiar"),
        clock=lambda: T0,
    )
    add_feedback(tmp_vault, "mejora", "Modo oscuro", "Quiero un modo oscuro.", clock=lambda: T0)
    return tmp_vault


def run(*args: str):
    return CliRunner().invoke(cli, ["feedback", *args])


def test_list_shows_one_line_per_item(vault: Vault) -> None:
    result = run("list")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines == [
        "fb-1\tnuevo\tbug\t2026-09-28 10:00\tEl micro se corta",
        "fb-2\tnuevo\tmejora\t2026-09-28 10:00\tModo oscuro",
    ]


def test_list_json_is_machine_readable(vault: Vault) -> None:
    result = run("list", "--json")

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    assert [item["id"] for item in items] == ["fb-1", "fb-2"]
    assert items[0]["context"]["mode"] == "estudiar"
    assert items[0]["body"] == "Al hablar mucho rato el micro se corta."


def test_mark_appends_a_status_change_and_list_folds_it(vault: Vault) -> None:
    result = run("mark", "fb-1", "--status", "triado", "--issue", "812")

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "fb-1\ttriado\tbug\t2026-09-28 10:00\tEl micro se corta\t#812"
    records = [line.root.record for line in read_jsonl(feedback_path(vault), FeedbackLine)]
    assert records == ["item", "item", "status"]  # appended, never rewritten
    assert get_feedback(vault, "fb-1").issue == 812

    listed = run("list", "--status", "triado")
    assert listed.output.splitlines() == [
        "fb-1\ttriado\tbug\t2026-09-28 10:00\tEl micro se corta\t#812"
    ]
    assert run("list", "--status", "nuevo").output.splitlines()[0].startswith("fb-2\t")


def test_mark_without_issue_keeps_the_triage_reference(vault: Vault) -> None:
    run("mark", "fb-1", "--status", "triado", "--issue", "812")

    result = run("mark", "fb-1", "--status", "nuevo")

    assert result.exit_code == 0, result.output
    item = get_feedback(vault, "fb-1")
    assert item.status == "nuevo" and item.issue == 812


def test_mark_descartado(vault: Vault) -> None:
    assert run("mark", "fb-2", "--status", "descartado").exit_code == 0
    assert [item.id for item in list_feedback(vault, "descartado")] == ["fb-2"]


def test_list_filter_with_nothing_matching(vault: Vault) -> None:
    result = run("list", "--status", "descartado")

    assert result.exit_code == 0
    assert result.output.strip() == "No hay comentarios en el buzón."
    assert json.loads(run("list", "--status", "descartado", "--json").output) == []


def test_mark_an_unknown_id_exits_1_and_writes_nothing(vault: Vault) -> None:
    before = feedback_path(vault).read_bytes()

    for feedback_id in ("fb-9", "nada"):
        result = run("mark", feedback_id, "--status", "triado")
        assert result.exit_code == 1
        assert f"No existe el comentario «{feedback_id}»" in result.output
    assert feedback_path(vault).read_bytes() == before


def test_mark_an_unknown_status_is_a_usage_error(vault: Vault) -> None:
    assert run("mark", "fb-1", "--status", "hecho").exit_code == 2
    assert run("mark", "fb-1", "--status", "triado", "--issue", "0").exit_code == 2


def test_mark_while_the_inbox_is_locked_exits_1(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studentassistant.vault.feedback.FEEDBACK_LOCK_TIMEOUT_SECONDS", 0.05)
    lock_path = vault_lock(vault.path, "feedback").path
    assert lock_path is not None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Another process (the running service) holding it: a second open file description's flock.
    with lock_path.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        result = run("mark", "fb-1", "--status", "triado")

    assert result.exit_code == 1
    assert "ocupado" in result.output
    assert get_feedback(vault, "fb-1").status == "nuevo"


def test_a_missing_vault_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "nope"))

    for args in (["list"], ["mark", "fb-1", "--status", "triado"]):
        result = run(*args)
        assert result.exit_code == 1
        assert "No se puede abrir la bóveda" in result.output
