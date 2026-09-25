"""Notes version tags `<subject-slug>/<topic-slug>/apuntes-vN`: next N, listing, pushed."""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import ManualClock, git

from studentassistant.config import VaultGitSettings
from studentassistant.vault import Vault, create_subject
from studentassistant.vault.sync import GitSync


@pytest.fixture
def sync(tmp_vault: Vault, git_origin: Path) -> GitSync:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    sync.checkpoint("primer commit")
    return sync


def test_versions_count_up_per_topic(tmp_vault: Vault, sync: GitSync) -> None:
    first = sync.create_notes_tag("fisica", "cinematica")
    create_subject(tmp_vault, "Física")  # pending: committed before the tag
    second = sync.create_notes_tag("fisica", "cinematica", "apuntes revisados")
    other = sync.create_notes_tag("fisica", "dinamica")

    assert (first.name, second.name, other.name) == (
        "fisica/cinematica/apuntes-v1",
        "fisica/cinematica/apuntes-v2",
        "fisica/dinamica/apuntes-v1",
    )
    assert second.commit == git(tmp_vault.path, "rev-parse", "HEAD").strip()
    assert first.commit != second.commit
    assert sync.list_notes_tags("fisica", "cinematica") == [first, second]
    assert sync.list_notes_tags("fisica", "energia") == []
    message = git(tmp_vault.path, "tag", "-l", "--format=%(contents:subject)", second.name)
    assert message.strip() == "apuntes revisados"


def test_the_next_version_follows_the_highest_existing_one(sync: GitSync) -> None:
    for _ in range(10):
        sync.create_notes_tag("fisica", "cinematica")

    assert sync.create_notes_tag("fisica", "cinematica").version == 11


def test_tags_are_pushed_with_the_normal_push(sync: GitSync, git_origin: Path) -> None:
    sync.push_now()
    tag = sync.create_notes_tag("fisica", "cinematica")  # on a commit the remote already has

    assert sync.push_now()

    assert git(git_origin, "rev-parse", f"{tag.name}^{{commit}}").strip() == tag.commit


def test_subjects_sharing_a_topic_slug_keep_separate_versions(sync: GitSync) -> None:
    fisica = [sync.create_notes_tag("fisica", "introduccion") for _ in range(2)]
    quimica = sync.create_notes_tag("quimica", "introduccion")

    assert quimica.name == "quimica/introduccion/apuntes-v1"
    assert sync.list_notes_tags("fisica", "introduccion") == fisica
    assert sync.list_notes_tags("quimica", "introduccion") == [quimica]
    assert sync.list_notes_tags("biologia", "introduccion") == []


@pytest.mark.parametrize(
    ("subject", "topic"), [("fisica", "Cinemática/../x"), ("fisica/x", "cinematica"), ("", "a")]
)
def test_subject_and_topic_slugs_are_required(sync: GitSync, subject: str, topic: str) -> None:
    with pytest.raises(ValueError):
        sync.create_notes_tag(subject, topic)
    with pytest.raises(ValueError):
        sync.list_notes_tags(subject, topic)
