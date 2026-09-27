"""The "versión de estudio" label (`editor.versions.mark_study_version`, #335)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from generate_topic import make_topic
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.versions import (
    NotesMissingError,
    list_versions,
    mark_study_version,
    read_study_label,
)
from studentassistant.vault import GitSync, Vault, notes_path, read_notes, write_notes
from studentassistant.vault.study import study_file_path


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _clock(minute: int):  # type: ignore[no-untyped-def]
    return lambda: datetime(2026, 9, 27, 10, minute, tzinfo=UTC)


def _head(sync: GitSync) -> str:
    return sync.git.check("rev-parse", "HEAD").stdout.strip()


def _dirty(sync: GitSync) -> str:
    return sync.git.check("status", "--porcelain").stdout.strip()


def test_the_unchanged_latest_version_is_labelled_without_a_new_tag(
    topic: ReviseTopic, sync: GitSync
) -> None:
    sync.create_notes_tag(topic.subject, topic.topic, "Apuntes v1")

    marked = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync, clock=_clock(1))

    assert (marked.version, marked.created_tag) == (1, False)
    assert marked.tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert [t.version for t in sync.list_notes_tags(topic.subject, topic.topic)] == [1]
    stored = read_study_label(topic.vault, topic.subject, topic.topic)
    assert stored is not None and stored.latest.version == 1 and len(stored.history) == 1
    assert stored.latest.marked_at == datetime(2026, 9, 27, 10, 1, tzinfo=UTC)
    path = study_file_path(topic.vault, topic.subject, topic.topic, "version")
    assert path.read_text(encoding="utf-8").startswith("latest:\n  version: 1\n")
    assert _dirty(sync) == ""  # committed

    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert [v.study for v in listing.versions] == [True]
    assert listing.study_version is not None and listing.study_version.version == 1
    assert listing.study_current is True


def test_notes_changed_since_the_latest_tag_get_a_new_version_first(
    topic: ReviseTopic, sync: GitSync
) -> None:
    sync.create_notes_tag(topic.subject, topic.topic, "Apuntes v1")
    edited = topic.notes.replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ o df/dx.")
    write_notes(topic.vault, topic.subject, topic.topic, edited)

    marked = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync)

    assert (marked.version, marked.created_tag) == (2, True)
    tags = sync.list_notes_tags(topic.subject, topic.topic)
    assert [t.version for t in tags] == [1, 2]
    assert tags[-1].message == f"Apuntes v2 de {topic.subject}/{topic.topic}: versión de estudio"
    relative = (
        notes_path(topic.vault, topic.subject, topic.topic).relative_to(topic.vault.path).as_posix()
    )
    assert sync.read_file_at(tags[-1].commit, relative) == edited
    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert [v.study for v in listing.versions] == [False, True]
    assert listing.study_current and not listing.changed_since_latest


def test_notes_with_no_tag_yet_become_v1(topic: ReviseTopic, sync: GitSync) -> None:
    marked = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync)
    assert (marked.version, marked.created_tag) == (1, True)


def test_marking_again_with_unchanged_notes_writes_nothing(
    topic: ReviseTopic, sync: GitSync
) -> None:
    first = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync, clock=_clock(1))
    head = _head(sync)

    again = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync, clock=_clock(9))

    assert again.version == first.version and again.marked_at == first.marked_at
    assert again.created_tag is False
    assert _head(sync) == head
    stored = read_study_label(topic.vault, topic.subject, topic.topic)
    assert stored is not None and len(stored.history) == 1


def test_an_edit_after_labelling_leaves_the_notes_editable_and_not_current(
    topic: ReviseTopic, sync: GitSync
) -> None:
    mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync)
    edited = topic.notes.replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ (prima).")
    write_notes(topic.vault, topic.subject, topic.topic, edited)  # nothing locks them
    assert read_notes(topic.vault, topic.subject, topic.topic) == edited

    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert listing.study_version is not None and listing.study_version.version == 1
    assert listing.study_current is False

    # Labelling again tags v2 and appends it to the history.
    marked = mark_study_version(topic.vault, topic.subject, topic.topic, sync=sync)
    assert (marked.version, marked.created_tag) == (2, True)
    stored = read_study_label(topic.vault, topic.subject, topic.topic)
    assert stored is not None and [e.version for e in stored.history] == [1, 2]
    listing = list_versions(topic.vault, topic.subject, topic.topic, sync=sync)
    assert [v.study for v in listing.versions] == [True, True] and listing.study_current


def test_no_notes_is_refused(tmp_vault: Vault, sync: GitSync) -> None:
    topic = make_topic(tmp_vault)
    with pytest.raises(NotesMissingError, match="Todavía no hay apuntes"):
        mark_study_version(tmp_vault, topic.subject, topic.topic, sync=sync)
    assert read_study_label(tmp_vault, topic.subject, topic.topic) is None
    listing = list_versions(tmp_vault, topic.subject, topic.topic, sync=sync)
    assert listing.study_version is None and listing.study_current is False
