"""Vault writers for the revision loop: fidelity mode, style guide, reverting paths."""

from __future__ import annotations

import subprocess

import pytest

from studentassistant.vault import (
    GitSync,
    RevertConflictError,
    Vault,
    create_subject,
    create_topic,
    get_subject,
    get_topic,
    notes_path,
    read_notes,
    set_fidelity_mode,
    set_style_guide,
    write_notes,
)


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def test_set_fidelity_mode_keeps_the_other_fields(tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "La Revolución").slug
    before = get_topic(tmp_vault, subject, topic).topic

    stored = set_fidelity_mode(tmp_vault, subject, topic, "ampliado")

    after = get_topic(tmp_vault, subject, topic).topic
    assert stored.topic == after and after.fidelity_mode == "ampliado"
    assert after.title == before.title and after.created_at == before.created_at
    with pytest.raises(ValueError):
        set_fidelity_mode(tmp_vault, subject, topic, "libre")


def test_set_style_guide_replaces_or_clears_it(tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Historia", style_guide="Fechas en negrita.").slug

    set_style_guide(tmp_vault, subject, "Fechas en negrita.\n- Sin abreviaturas.\n")
    assert get_subject(tmp_vault, subject).subject.style_guide == (
        "Fechas en negrita.\n- Sin abreviaturas.\n"
    )
    set_style_guide(tmp_vault, subject, "  ")
    assert get_subject(tmp_vault, subject).subject.style_guide is None
    assert get_subject(tmp_vault, subject).subject.name == "Historia"


def test_revert_paths_undoes_only_the_named_paths(tmp_vault: Vault) -> None:
    sync = GitSync(tmp_vault)
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "La Revolución").slug
    write_notes(tmp_vault, subject, topic, "# v1\n")
    sync.checkpoint("v1")
    write_notes(tmp_vault, subject, topic, "# v2\n")
    set_fidelity_mode(tmp_vault, subject, topic, "ampliado")
    other = tmp_vault.path / "other.txt"
    other.write_text("carried along\n", encoding="utf-8")
    commit = sync.checkpoint("v2")
    assert commit is not None
    notes = notes_path(tmp_vault, subject, topic).relative_to(tmp_vault.path).as_posix()
    topic_file = f"subjects/{subject}/topics/{topic}/topic.yaml"

    reverted = sync.revert_paths(commit, [notes, topic_file], "Deshecho")

    assert reverted is not None
    assert read_notes(tmp_vault, subject, topic) == "# v1\n"
    assert get_topic(tmp_vault, subject, topic).topic.fidelity_mode == "estricto"
    assert other.read_text(encoding="utf-8") == "carried along\n"
    assert _git(tmp_vault, "log", "-1", "--format=%s").strip() == "Deshecho"
    assert _git(tmp_vault, "status", "--porcelain").strip() == ""
    # Nothing left to undo the second time; and a later change blocks it.
    with pytest.raises(RevertConflictError):
        sync.revert_paths(commit, [notes], "otra vez")


def test_revert_paths_removes_a_file_the_commit_created(tmp_vault: Vault) -> None:
    sync = GitSync(tmp_vault)
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "La Revolución").slug
    sync.checkpoint("topic")
    write_notes(tmp_vault, subject, topic, "# v1\n")
    commit = sync.checkpoint("notes")
    assert commit is not None
    notes = notes_path(tmp_vault, subject, topic).relative_to(tmp_vault.path).as_posix()

    assert sync.revert_paths(commit, [notes], "Deshecho") is not None
    assert read_notes(tmp_vault, subject, topic) is None


def test_revert_paths_refuses_bad_input(tmp_vault: Vault) -> None:
    sync = GitSync(tmp_vault)
    with pytest.raises(ValueError):
        sync.revert_paths("0" * 40, ["a.txt"], "x")
    with pytest.raises(ValueError):
        sync.revert_paths("HEAD", ["../fuera"], "x")
