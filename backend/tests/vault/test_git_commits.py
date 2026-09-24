"""Batched commits after a quiet period, checkpoints, aggregated Spanish messages."""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import ManualClock, commit_count, git

from studentassistant.config import Settings, VaultGitSettings
from studentassistant.vault import Vault, create_subject, create_topic, put_source, start_session
from studentassistant.vault.sync import GitSync, summarize_changes


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def sync(tmp_vault: Vault, clock: ManualClock) -> GitSync:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=clock)
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    sync.checkpoint("materia y tema")
    return sync


def subject_line(vault: Vault) -> str:
    return git(vault.path, "log", "-1", "--format=%s").strip()


def test_changes_commit_after_the_quiet_period(
    tmp_vault: Vault, sync: GitSync, clock: ManualClock
) -> None:
    session = start_session(tmp_vault, "fisica", "cinematica", "pc", "1")
    sync.checkpoint("sesión iniciada")
    before = commit_count(tmp_vault.path)
    for index in range(12):
        session.append_transcript(index * 1000, index * 1000 + 900, f"frase {index}")
        sync.note_change()
    for page in range(2):
        put_source(tmp_vault, "fisica", "cinematica", "notes", "foto.jpg", b"jpeg", {"n": page})
        sync.note_change()

    clock.advance(29)
    sync.run_due()
    assert commit_count(tmp_vault.path) == before
    assert sync.status().pending_changes

    clock.advance(1)
    sync.run_due()
    assert commit_count(tmp_vault.path) == before + 1
    assert subject_line(tmp_vault) == f"sesión {session.id}: 12 segmentos, 2 capturas"
    assert not sync.status().pending_changes
    assert git(tmp_vault.path, "status", "--porcelain") == ""


def test_a_new_change_restarts_the_quiet_period(
    tmp_vault: Vault, sync: GitSync, clock: ManualClock
) -> None:
    before = commit_count(tmp_vault.path)
    create_topic(tmp_vault, "fisica", "Dinámica")
    sync.note_change()
    clock.advance(20)
    create_topic(tmp_vault, "fisica", "Energía")
    sync.note_change()
    clock.advance(20)
    sync.run_due()
    assert commit_count(tmp_vault.path) == before

    clock.advance(10)
    sync.run_due()
    assert commit_count(tmp_vault.path) == before + 1
    assert subject_line(tmp_vault) == "2 archivos cambiados"


def test_a_steady_stream_is_committed_by_the_max_delay(
    tmp_vault: Vault, sync: GitSync, clock: ManualClock
) -> None:
    session = start_session(tmp_vault, "fisica", "cinematica", "pc", "1")
    before = commit_count(tmp_vault.path)
    for index in range(30):  # one segment every 10 s for 300 s: never 30 s of quiet
        session.append_transcript(index, index + 1, f"frase {index}")
        sync.note_change()
        clock.advance(10)
        sync.run_due()

    assert commit_count(tmp_vault.path) == before + 1


def test_checkpoint_commits_at_once_with_the_summary_in_the_body(
    tmp_vault: Vault, sync: GitSync
) -> None:
    session = start_session(tmp_vault, "fisica", "cinematica", "pc", "1")
    session.append_event("capture.stored", "phone")
    sync.note_change()

    sha = sync.checkpoint("captura guardada")

    assert sha == git(tmp_vault.path, "rev-parse", "HEAD").strip()
    assert subject_line(tmp_vault) == "captura guardada"
    body = git(tmp_vault.path, "log", "-1", "--format=%b").strip()
    assert body == f"sesión {session.id}: 1 evento"
    assert not sync.status().pending_changes
    assert sync.status().last_commit == sha


def test_nothing_to_commit_makes_no_empty_commit(
    tmp_vault: Vault, sync: GitSync, clock: ManualClock
) -> None:
    before = commit_count(tmp_vault.path)

    assert sync.checkpoint("nada") is None
    sync.note_change()
    clock.advance(60)
    sync.run_due()

    assert commit_count(tmp_vault.path) == before


def test_a_failed_commit_keeps_the_batch_pending(
    tmp_vault: Vault, sync: GitSync, clock: ManualClock
) -> None:
    create_topic(tmp_vault, "fisica", "Dinámica")
    sync.note_change()
    lock = tmp_vault.path / ".git" / "index.lock"
    lock.write_text("")
    clock.advance(30)

    sync.run_due()  # never raises

    status = sync.status()
    assert status.pending_changes
    assert status.last_error is not None
    lock.unlink()
    sync.run_due()
    assert not sync.status().pending_changes
    assert sync.status().last_error is None


def test_summarize_changes() -> None:
    base = "subjects/f/topics/t/sessions/20260924-183000"
    added = {
        f"{base}/transcript.jsonl": 1,
        f"{base}/events.jsonl": 4,
        "subjects/f/topics/t/sources/web/001-x.yaml": 3,
        "subjects/f/topics/t/sources/web/001-x.md": 10,
    }
    new = {"subjects/f/topics/t/sources/web/001-x.yaml"}

    assert summarize_changes(added, new) == (
        "sesión 20260924-183000: 1 segmento, 4 eventos, 1 fuente"
    )
    assert summarize_changes({"vault.yaml": 3}, {"vault.yaml"}) == "1 archivo cambiado"
    other = "subjects/f/topics/t/sessions/20260924-190000/transcript.jsonl"
    assert summarize_changes({f"{base}/transcript.jsonl": 2, other: 1}, set()) == (
        "sesión 20260924-183000: 2 segmentos; sesión 20260924-190000: 1 segmento"
    )


def test_the_timings_come_from_the_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("SA_VAULT__GIT__COMMIT_QUIET_SECONDS", "5")
    monkeypatch.setenv("SA_VAULT__GIT__AUTHOR_EMAIL", "ana@example.invalid")

    settings = Settings().vault.git

    assert settings.commit_quiet_seconds == 5
    assert settings.author_email == "ana@example.invalid"
    assert settings.push_debounce_seconds == 120
    assert VaultGitSettings().commit_quiet_seconds == 30
