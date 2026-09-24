"""Vault writers: an atomic write leaves nothing behind it, and a dump is always the same bytes."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from studentassistant.vault import files
from studentassistant.vault.files import read_yaml, write_text_atomic, write_yaml_atomic
from studentassistant.vault.models import Subject, Topic

TOPIC = Topic(
    title="Límites y continuidad",
    created_at=datetime(2026, 9, 24, 18, 30, tzinfo=UTC),
    sessions=["20260924-183000"],
)


def test_a_successful_write_leaves_only_the_file_it_wrote(tmp_path: Path) -> None:
    write_text_atomic(tmp_path / "apuntes.md", "# Apuntes\n")

    assert [entry.name for entry in tmp_path.iterdir()] == ["apuntes.md"]
    assert (tmp_path / "apuntes.md").read_text(encoding="utf-8") == "# Apuntes\n"


def test_a_successful_yaml_write_leaves_no_temporary_file_next_to_it(tmp_path: Path) -> None:
    write_yaml_atomic(tmp_path / "topic.yaml", TOPIC)

    assert [entry.name for entry in tmp_path.iterdir()] == ["topic.yaml"]


def test_a_write_replaces_the_content_that_was_there(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"
    write_text_atomic(target, "antes\n")

    write_text_atomic(target, "después\n")

    assert target.read_text(encoding="utf-8") == "después\n"
    assert [entry.name for entry in tmp_path.iterdir()] == ["topic.yaml"]


def test_a_write_that_fails_before_the_rename_keeps_the_old_content_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "topic.yaml"
    write_text_atomic(target, "antes\n")

    def refuse_to_replace(*arguments: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(files.os, "replace", refuse_to_replace)

    with pytest.raises(OSError, match="no space left"):
        write_text_atomic(target, "después\n")

    assert target.read_text(encoding="utf-8") == "antes\n"
    assert [entry.name for entry in tmp_path.iterdir()] == ["topic.yaml"]


def test_a_write_into_a_directory_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_text_atomic(tmp_path / "topics" / "topic.yaml", "uno\n")

    assert not (tmp_path / "topics").exists()


def test_a_write_fsyncs_the_content_and_then_the_directory_it_landed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[str] = []

    def record(descriptor: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{descriptor}"))

    monkeypatch.setattr(files.os, "fsync", record)

    write_text_atomic(tmp_path / "topic.yaml", "uno\n")

    assert len(synced) == 2
    assert synced[0].endswith(".tmp"), "the content is fsynced while its temporary file is open"
    assert synced[1] == str(tmp_path), "and the directory entry the rename created is fsynced too"


def test_a_written_file_is_readable_only_by_the_student(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"

    write_text_atomic(target, "uno\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_dumping_the_same_model_twice_gives_byte_identical_files(tmp_path: Path) -> None:
    first = tmp_path / "primero.yaml"
    second = tmp_path / "segundo.yaml"

    write_yaml_atomic(first, TOPIC)
    write_yaml_atomic(second, Topic.model_validate(TOPIC.model_dump()))

    assert first.read_bytes() == second.read_bytes()


def test_a_round_trip_through_read_yaml_returns_an_equal_model(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"

    write_yaml_atomic(target, TOPIC)

    assert read_yaml(target, Topic) == TOPIC


def test_the_yaml_holds_the_fields_in_the_order_the_model_declares_them(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"

    write_yaml_atomic(target, TOPIC)

    lines = target.read_text(encoding="utf-8").splitlines()
    top_level_keys = [line.split(":", 1)[0] for line in lines if not line.startswith(("-", " "))]
    assert top_level_keys == list(Topic.model_fields)


def test_the_yaml_carries_no_document_marker_and_ends_with_a_newline(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"

    write_yaml_atomic(target, TOPIC)

    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] != "---"
    assert lines[-1] != "..."
    assert text.endswith("\n")


def test_the_yaml_keeps_the_accents_of_the_spanish_it_holds(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"

    write_yaml_atomic(target, TOPIC)

    assert "Límites y continuidad" in target.read_text(encoding="utf-8")


def test_a_long_value_stays_on_one_line_so_an_edit_to_it_touches_one_line(tmp_path: Path) -> None:
    guide = " ".join(["Explica cada símbolo antes de usarlo"] * 12)
    target = tmp_path / "subject.yaml"

    write_yaml_atomic(target, Subject(name="Matemáticas", style_guide=guide))

    text = target.read_text(encoding="utf-8")
    assert len(text.splitlines()) == len(Subject.model_fields)
    assert read_yaml(target, Subject) == Subject(name="Matemáticas", style_guide=guide)


def test_read_yaml_of_an_empty_file_is_a_validation_error_not_a_silent_none(tmp_path: Path) -> None:
    target = tmp_path / "topic.yaml"
    target.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="Input should be a valid dictionary"):
        read_yaml(target, Topic)
