"""Writing the master notes and their draft (`write_notes`, `write_notes_draft`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import (
    NotesError,
    SecretRefused,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    notes_draft_path,
    notes_path,
    read_notes,
    read_notes_draft,
    topic_directory,
    write_notes,
    write_notes_draft,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Reconquista").slug


def test_write_notes_creates_the_notes_directory_and_reads_back(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    path = write_notes(tmp_vault, *topic, "# La Reconquista\n")
    assert path == notes_path(tmp_vault, *topic)
    assert read_notes(tmp_vault, *topic) == "# La Reconquista\n"
    assert write_notes(tmp_vault, *topic, "# v2\n") == path
    assert read_notes(tmp_vault, *topic) == "# v2\n"


def test_a_draft_leaves_the_notes_alone_and_valid_notes_remove_it(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    write_notes(tmp_vault, *topic, "# válidos\n")
    path = write_notes_draft(tmp_vault, *topic, "# borrador\n")
    assert path == notes_draft_path(tmp_vault, *topic)
    assert path.name == "borrador.md"
    assert read_notes(tmp_vault, *topic) == "# válidos\n"
    assert read_notes_draft(tmp_vault, *topic) == "# borrador\n"

    write_notes(tmp_vault, *topic, "# nuevos\n")
    assert read_notes_draft(tmp_vault, *topic) is None
    assert not path.exists()


def test_writing_into_an_unknown_topic_is_refused(tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Historia").slug
    with pytest.raises(TopicNotFoundError):
        write_notes(tmp_vault, subject, "no-existe", "# x\n")
    with pytest.raises(TopicNotFoundError):
        write_notes_draft(tmp_vault, subject, "../fuera", "# x\n")


def test_notes_that_look_like_a_key_are_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    with pytest.raises(SecretRefused):
        write_notes(tmp_vault, *topic, f"clave {ANTHROPIC_KEY}\n")
    assert read_notes(tmp_vault, *topic) is None


def test_a_symlinked_notes_directory_is_refused(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (topic_directory(tmp_vault, *topic) / "notes").symlink_to(outside)
    with pytest.raises(NotesError):
        write_notes(tmp_vault, *topic, "# x\n")
    with pytest.raises(NotesError):
        write_notes_draft(tmp_vault, *topic, "# x\n")
    assert list(outside.iterdir()) == []
