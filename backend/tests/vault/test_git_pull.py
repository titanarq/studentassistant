"""`sync()`: pull --rebase between two clones, JSONL union merge, clean abort on conflicts."""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import ManualClock, clone_vault, git

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    resume_session,
    start_session,
)
from studentassistant.vault.sync import GitSync


@pytest.fixture
def pc_a(tmp_vault: Vault, git_origin: Path) -> GitSync:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    session = start_session(tmp_vault, "fisica", "cinematica", "pc-a", "1")
    session.append_transcript(0, 900, "primera frase")
    sync.flush()
    return sync


@pytest.fixture
def pc_b(pc_a: GitSync, git_origin: Path, tmp_path: Path) -> GitSync:
    return GitSync(clone_vault(git_origin, tmp_path / "pc-b"), VaultGitSettings(), ManualClock())


def head(sync: GitSync) -> str:
    return git(sync.vault.path, "rev-parse", "HEAD").strip()


def test_jsonl_divergence_merges_by_union(pc_a: GitSync, pc_b: GitSync) -> None:
    on_a = resume_session(pc_a.vault, "fisica", "cinematica")
    on_b = resume_session(pc_b.vault, "fisica", "cinematica")
    on_a.append_transcript(1000, 1900, "frase en A")
    pc_a.flush()
    on_b.append_transcript(1000, 1900, "frase en B")
    pc_b.checkpoint("en B")

    result = pc_b.sync()

    assert result.ok, result
    texts = [segment.text for segment in on_b.read_transcript()]
    assert sorted(texts) == ["frase en A", "frase en B", "primera frase"]
    assert pc_b.status().last_sync == result
    assert pc_b.status().pending_commits == 1
    assert pc_b.push_now()
    assert pc_a.sync().ok
    assert head(pc_a) == head(pc_b)


def test_a_conflict_outside_jsonl_aborts_and_keeps_everything(pc_a: GitSync, pc_b: GitSync) -> None:
    subject_a = pc_a.vault.path / "subjects" / "fisica" / "subject.yaml"
    subject_b = pc_b.vault.path / "subjects" / "fisica" / "subject.yaml"
    subject_a.write_text("name: Física A\nstyle_guide: null\n")
    pc_a.flush()
    subject_b.write_text("name: Física B\nstyle_guide: null\n")
    local = pc_b.checkpoint("cambio en B")

    result = pc_b.sync()

    assert result.outcome == "conflict"
    assert result.conflicts == ("subjects/fisica/subject.yaml",)
    assert head(pc_b) == local
    assert subject_b.read_text() == "name: Física B\nstyle_guide: null\n"
    assert git(pc_b.vault.path, "status", "--porcelain") == ""
    rebase_dir = git(pc_b.vault.path, "rev-parse", "--git-path", "rebase-merge").strip()
    assert not (pc_b.vault.path / rebase_dir).exists()
    assert pc_b.status().last_sync == result


def test_pending_changes_are_committed_before_pulling(pc_a: GitSync, pc_b: GitSync) -> None:
    create_topic(pc_b.vault, "fisica", "Dinámica")
    pc_b.note_change()
    create_topic(pc_a.vault, "fisica", "Energía")
    pc_a.flush()

    assert pc_b.sync().ok

    assert git(pc_b.vault.path, "status", "--porcelain") == ""
    topics = pc_b.vault.path / "subjects" / "fisica" / "topics"
    assert {path.name for path in topics.iterdir()} == {"cinematica", "dinamica", "energia"}


def test_an_unreachable_remote_is_reported(pc_a: GitSync, git_origin: Path) -> None:
    git_origin.rename(git_origin.with_name("gone.git"))

    result = pc_a.sync()

    assert result.outcome == "offline"
    assert pc_a.status().last_sync == result


def test_an_empty_remote_is_nothing_to_pull(tmp_vault: Vault, git_origin: Path) -> None:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    create_subject(tmp_vault, "Física")

    result = sync.sync()

    assert result.ok
    assert sync.status().pending_commits == 1
    assert sync.status().next_push_due is not None
