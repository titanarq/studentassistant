"""`studentassistant purge [--topic] [--dry-run] [--hard]` on a tmp vault with a bare remote."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.config import VaultGitSettings
from studentassistant.observer import (
    COMPACTED_EVENT_KIND,
    SEGMENT_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    fold,
    load_observer_snapshot,
)
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    read_topic_events,
    start_session,
    topic_directory,
)

TOPIC = "matematicas/derivadas"
BURST = "subjects/matematicas/topics/derivadas/sources/notes/page-001.burst1.jpg"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def vault(
    tmp_vault: Vault, git_origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Vault:
    """`tmp_vault` as the configured vault, with one accepted topic pushed to `git_origin`."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    subject = create_subject(tmp_vault, "Matemáticas").slug
    slug = create_topic(tmp_vault, subject, "Derivadas").slug
    create_topic(tmp_vault, subject, "Integrales")
    session = start_session(tmp_vault, subject, slug, host="ubuntu-pc", protocol_version="1.0")
    session.append_event("session.started", "phone")
    session.append_event(SEGMENT_EVENT_KIND, "stt", {"segment_id": "s1"})
    session.append_event(
        STATE_OP_EVENT_KIND, "observer", {"op": "add_section", "section_id": "a", "title": "A"}
    )
    session.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "assign_segments", "section_id": "a", "segment_ids": ["s1"]},
    )
    session.append_transcript(0, 900, "la derivada")
    end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
    put_source(
        tmp_vault,
        subject,
        slug,
        "notes",
        "still.jpg",
        b"page",
        {"capture_id": "c"},
        derived={"burst1.jpg": b"\xff\xd8" + b"x" * 4096},
    )
    notes = topic_directory(tmp_vault, subject, slug) / "notes" / "apuntes.md"
    notes.parent.mkdir()
    notes.write_text("# Derivadas\n\nTexto.[^p1]\n\n[^p1]: [p1](../sources/notes/page-001.jpg)\n")
    sync = GitSync(tmp_vault, VaultGitSettings())
    sync.checkpoint("tema")
    sync.create_notes_tag(slug)
    sync.push_now()
    return tmp_vault


def test_dry_run_lists_and_changes_nothing(vault: Vault) -> None:
    head = git(vault.path, "rev-parse", "HEAD")

    result = CliRunner().invoke(cli, ["purge", "--dry-run", "--hard"])

    assert result.exit_code == 0, result.output
    assert f"{BURST}: se borra (original de ráfaga, 4.0 KB)" in result.output
    assert "events.jsonl: se compacta (eventos ya plegados en la instantánea" in result.output
    assert "matematicas/integrales: se omite, aún no tiene apuntes." in result.output
    assert "Simulación: se liberarían" in result.output
    assert "Con --hard se borrarían del historial 1 archivos." in result.output
    assert git(vault.path, "rev-parse", "HEAD") == head
    assert git(vault.path, "status", "--porcelain") == ""


def test_soft_purge_commits_compacts_and_pushes(vault: Vault, git_origin: Path) -> None:
    expected = fold(read_topic_events(vault, "matematicas", "derivadas"))

    result = CliRunner().invoke(cli, ["purge", "--topic", TOPIC])

    assert result.exit_code == 0, result.output
    assert "Purga guardada en" in result.output
    assert "Subida a GitHub." in result.output
    assert not (vault.path / BURST).exists()
    events = list(read_topic_events(vault, "matematicas", "derivadas"))
    assert [event.kind for _, event in events] == [COMPACTED_EVENT_KIND]
    assert fold(events) == expected
    assert load_observer_snapshot(vault, "matematicas", "derivadas").state == expected
    assert git(vault.path, "status", "--porcelain") == ""
    assert git(vault.path, "rev-parse", "HEAD") == git(git_origin, "rev-parse", "main")
    # Recoverable: the burst is still in history.
    assert BURST in git(vault.path, "rev-list", "--objects", "--all")


def test_hard_purge_asks_before_rewriting(vault: Vault) -> None:
    head = git(vault.path, "rev-parse", "HEAD")

    result = CliRunner().invoke(cli, ["purge", "--hard"], input="no\n")

    assert result.exit_code == 1
    assert "clonarla de nuevo" in result.output
    assert "Cancelado" in result.output
    assert git(vault.path, "rev-parse", "HEAD") == head
    assert (vault.path / BURST).exists()


def test_hard_purge_rewrites_and_force_pushes(vault: Vault, git_origin: Path) -> None:
    result = CliRunner().invoke(cli, ["purge", "--hard"], input="reescribir\n")

    assert result.exit_code == 0, result.output
    assert "Historial reescrito: 1 archivos fuera de todas las versiones" in result.output
    assert "Subido a GitHub a la fuerza" in result.output
    for repository in (vault.path, git_origin):
        assert BURST not in git(repository, "rev-list", "--objects", "--all")
    assert git(vault.path, "rev-parse", "HEAD") == git(git_origin, "rev-parse", "main")


def test_an_unknown_topic_is_refused(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["purge", "--topic", "matematicas/nada"])

    assert result.exit_code == 1
    assert "No existe el tema" in result.output
