"""Reading the master notes and listing generated material, before and after either exists."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.vault import (
    NotesError,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    generated_directory,
    list_generated,
    notes_path,
    read_notes,
    topic_directory,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Reconquista").slug


def test_notes_not_written_yet_are_none_and_nothing_is_created(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    assert read_notes(tmp_vault, *topic) is None
    assert not (topic_directory(tmp_vault, *topic) / "notes").exists()


def test_notes_are_read_as_utf8_text(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    path = notes_path(tmp_vault, *topic)
    assert path == topic_directory(tmp_vault, *topic) / "notes" / "apuntes.md"
    path.parent.mkdir()
    path.write_text("# La Reconquista\n\nCovadonga, año 722.\n", encoding="utf-8")
    assert read_notes(tmp_vault, *topic) == "# La Reconquista\n\nCovadonga, año 722.\n"


def test_notes_that_are_a_symlink_are_refused(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    outside = tmp_path / "fuera.md"
    outside.write_text("fuera", encoding="utf-8")
    path = notes_path(tmp_vault, *topic)
    path.parent.mkdir()
    path.symlink_to(outside)
    with pytest.raises(NotesError):
        read_notes(tmp_vault, *topic)


def test_no_generated_directory_lists_as_empty(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    assert list_generated(tmp_vault, *topic) == []
    assert not generated_directory(tmp_vault, *topic).exists()


def test_generated_files_are_listed_sorted_and_vault_relative(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    root = generated_directory(tmp_vault, *topic)
    (root / "quiz").mkdir(parents=True)
    (root / "outline.md").write_text("# Esquema", encoding="utf-8")
    (root / "quiz" / "quiz.yaml").write_text("items: []\n", encoding="utf-8")
    (root / "flashcards.apkg").write_bytes(b"PK")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("x", encoding="utf-8")
    (root / "link-dir").symlink_to(outside)
    (root / "link.md").symlink_to(outside / "leak.md")

    base = f"subjects/{topic[0]}/topics/{topic[1]}/generated"
    assert list_generated(tmp_vault, *topic) == [
        f"{base}/flashcards.apkg",
        f"{base}/outline.md",
        f"{base}/quiz/quiz.yaml",
    ]


@pytest.mark.parametrize("reader", [read_notes, list_generated])
def test_readers_refuse_what_is_not_a_topic(
    tmp_vault: Vault, topic: tuple[str, str], reader: object
) -> None:
    with pytest.raises(SubjectNotFoundError):
        reader(tmp_vault, "..", topic[1])  # type: ignore[operator]
    with pytest.raises(TopicNotFoundError):
        reader(tmp_vault, topic[0], "../la-reconquista")  # type: ignore[operator]
    with pytest.raises(TopicNotFoundError):
        reader(tmp_vault, topic[0], "otro")  # type: ignore[operator]
