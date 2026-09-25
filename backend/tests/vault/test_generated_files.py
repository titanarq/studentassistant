"""Writing, reading and removing files under a topic's `generated/` (`vault/notes.py`)."""

from __future__ import annotations

import pytest

from studentassistant.vault import (
    NotesError,
    SecretRefused,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    generated_directory,
    list_generated,
    read_generated,
    remove_generated,
    write_generated,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def test_write_read_and_list(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, name = topic
    text_path = write_generated(tmp_vault, subject, name, "quiz.yaml", "preguntas: []\n")
    binary_path = write_generated(tmp_vault, subject, name, "slides/deck.pdf", b"%PDF-1.7")

    assert text_path == generated_directory(tmp_vault, subject, name) / "quiz.yaml"
    assert read_generated(tmp_vault, subject, name, "quiz.yaml") == b"preguntas: []\n"
    assert read_generated(tmp_vault, subject, name, "slides/deck.pdf") == b"%PDF-1.7"
    assert binary_path.parent.name == "slides"
    assert read_generated(tmp_vault, subject, name, "nada.md") is None
    base = f"subjects/{subject}/topics/{name}/generated"
    assert list_generated(tmp_vault, subject, name) == [
        f"{base}/quiz.yaml",
        f"{base}/slides/deck.pdf",
    ]


def test_remove_prunes_empty_directories(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, name = topic
    write_generated(tmp_vault, subject, name, "a/b/c.md", "x")
    write_generated(tmp_vault, subject, name, "a/d.md", "y")

    assert remove_generated(tmp_vault, subject, name, "a/b/c.md")
    assert not remove_generated(tmp_vault, subject, name, "a/b/c.md")

    root = generated_directory(tmp_vault, subject, name)
    assert not (root / "a" / "b").exists() and (root / "a" / "d.md").is_file()
    assert remove_generated(tmp_vault, subject, name, "a/d.md")
    assert not (root / "a").exists() and root.is_dir()


@pytest.mark.parametrize(
    "bad", ["", "/abs.md", "../fuera.md", "a/../b.md", ".oculto", "a//b", "a b"]
)
def test_names_outside_generated_are_refused(
    tmp_vault: Vault, topic: tuple[str, str], bad: str
) -> None:
    subject, name = topic
    with pytest.raises(NotesError):
        write_generated(tmp_vault, subject, name, bad, "x")
    with pytest.raises(NotesError):
        read_generated(tmp_vault, subject, name, bad)


def test_a_symlinked_directory_is_refused(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path
) -> None:
    subject, name = topic
    root = generated_directory(tmp_vault, subject, name)
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "enlace").symlink_to(outside)
    with pytest.raises(NotesError, match="symlink"):
        write_generated(tmp_vault, subject, name, "enlace/x.md", "x")
    assert list(outside.iterdir()) == []


def test_an_unknown_topic_and_a_secret_are_refused(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, name = topic
    with pytest.raises(TopicNotFoundError):
        write_generated(tmp_vault, subject, "no-existe", "quiz.yaml", "x")
    with pytest.raises(SecretRefused):
        write_generated(tmp_vault, subject, name, "quiz.yaml", "sk-ant-api03-" + "a" * 90)
    assert read_generated(tmp_vault, subject, name, "quiz.yaml") is None
