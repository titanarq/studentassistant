"""Notes version tags `<topic-slug>/apuntes-vN`: next N, listing, pushed with the branch."""

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
    first = sync.create_notes_tag("cinematica")
    create_subject(tmp_vault, "Física")  # pending: committed before the tag
    second = sync.create_notes_tag("cinematica", "apuntes revisados")
    other = sync.create_notes_tag("dinamica")

    assert (first.name, second.name, other.name) == (
        "cinematica/apuntes-v1",
        "cinematica/apuntes-v2",
        "dinamica/apuntes-v1",
    )
    assert second.commit == git(tmp_vault.path, "rev-parse", "HEAD").strip()
    assert first.commit != second.commit
    assert sync.list_notes_tags("cinematica") == [first, second]
    assert sync.list_notes_tags("energia") == []
    message = git(tmp_vault.path, "tag", "-l", "--format=%(contents:subject)", second.name)
    assert message.strip() == "apuntes revisados"


def test_the_next_version_follows_the_highest_existing_one(sync: GitSync) -> None:
    for _ in range(10):
        sync.create_notes_tag("cinematica")

    assert sync.create_notes_tag("cinematica").version == 11


def test_tags_are_pushed_with_the_normal_push(sync: GitSync, git_origin: Path) -> None:
    sync.push_now()
    tag = sync.create_notes_tag("cinematica")  # on a commit the remote already has

    assert sync.push_now()

    assert git(git_origin, "rev-parse", f"{tag.name}^{{commit}}").strip() == tag.commit


def test_a_topic_slug_is_required(sync: GitSync) -> None:
    with pytest.raises(ValueError):
        sync.create_notes_tag("Cinemática/../x")
