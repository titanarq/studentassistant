"""One active writer between two clones of one bare repository, and divergence kept on both sides.

`pc_a` is `tmp_vault` pushing to `git_origin`; `pc_b` is a second clone of it. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from git_helpers import ManualClock, clone_vault, git

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    GitSync,
    Vault,
    check_active_host,
    claim_active_host,
    create_subject,
    create_topic,
    read_active_host,
    release_active_host,
)
from studentassistant.vault.sync import DIVERGENCE_LOCAL_REF, DIVERGENCE_REMOTE_REF

STALE = VaultGitSettings().active_host_stale_seconds
SUBJECT = Path("subjects/fisica/subject.yaml")


@pytest.fixture
def pc_a(tmp_vault: Vault, git_origin: Path) -> GitSync:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    sync.flush()
    return sync


@pytest.fixture
def pc_b(pc_a: GitSync, git_origin: Path, tmp_path: Path) -> GitSync:
    return GitSync(clone_vault(git_origin, tmp_path / "pc-b"), VaultGitSettings(), ManualClock())


def refs(sync: GitSync) -> set[str]:
    return set(git(sync.vault.path, "for-each-ref", "--format=%(refname)").split())


def test_a_claim_pushed_by_one_pc_warns_the_other_after_its_pull(
    pc_a: GitSync, pc_b: GitSync
) -> None:
    claim_active_host(pc_a.vault, "pc-a", "20260925-100000", "fisica", "cinematica")
    pc_a.flush()

    assert check_active_host(pc_b.vault, "pc-b", STALE) is None  # not pulled yet
    assert pc_b.sync().ok

    warning = check_active_host(pc_b.vault, "pc-b", STALE)
    assert warning is not None
    assert warning.record.host == "pc-a"
    assert check_active_host(pc_a.vault, "pc-a", STALE) is None  # never about oneself


def test_the_release_pushed_at_session_end_clears_the_warning(pc_a: GitSync, pc_b: GitSync) -> None:
    claim_active_host(pc_a.vault, "pc-a", "s1")
    pc_a.flush()
    assert pc_b.sync().ok
    assert check_active_host(pc_b.vault, "pc-b", STALE) is not None

    assert release_active_host(pc_a.vault, "pc-a", "s1") is not None
    pc_a.flush()
    assert pc_b.sync().ok

    assert check_active_host(pc_b.vault, "pc-b", STALE) is None


def test_a_stale_claim_does_not_warn(pc_a: GitSync, pc_b: GitSync) -> None:
    long_ago = datetime.now(UTC) - timedelta(seconds=STALE + 60)
    claim_active_host(pc_a.vault, "pc-a", "s1", claimed_at=long_ago)
    pc_a.flush()
    assert pc_b.sync().ok

    assert read_active_host(pc_b.vault) is not None
    assert check_active_host(pc_b.vault, "pc-b", STALE) is None


def test_both_pcs_claiming_is_never_a_conflict_and_the_remote_claim_is_kept(
    pc_a: GitSync, pc_b: GitSync
) -> None:
    claim_active_host(pc_b.vault, "pc-b", "s-b")
    pc_b.checkpoint("claim on B")  # not pushed: B went offline
    claim_active_host(pc_a.vault, "pc-a", "s-a")
    pc_a.flush()

    result = pc_b.sync()

    assert result.ok, result
    record = read_active_host(pc_b.vault)
    assert record is not None and record.host == "pc-a"
    assert git(pc_b.vault.path, "status", "--porcelain") == ""
    assert pc_b.status().divergence is None


def test_request_push_makes_the_next_run_due_push(pc_a: GitSync, git_origin: Path) -> None:
    claim_active_host(pc_a.vault, "pc-a", "s1")
    local = pc_a.checkpoint("claim")
    assert pc_a.status().next_push_due is not None
    pc_a.run_due()
    assert git(git_origin, "rev-parse", "main").strip() != local  # debounced, not pushed yet

    pc_a.request_push()
    pc_a.run_due()

    assert git(git_origin, "rev-parse", "main").strip() == local


def test_a_divergence_is_reported_with_both_versions_kept(pc_a: GitSync, pc_b: GitSync) -> None:
    (pc_a.vault.path / SUBJECT).write_text("name: Física A\nstyle_guide: null\n")
    pc_a.flush()
    remote = git(pc_a.vault.path, "rev-parse", "HEAD").strip()
    (pc_b.vault.path / SUBJECT).write_text("name: Física B\nstyle_guide: null\n")
    local = pc_b.checkpoint("cambio en B")
    assert local is not None

    result = pc_b.sync()

    assert result.outcome == "conflict"
    divergence = pc_b.status().divergence
    assert divergence is not None
    assert divergence.paths == (str(SUBJECT),)
    assert (divergence.local_commit, divergence.remote_commit) == (local, remote)
    assert git(pc_b.vault.path, "rev-parse", DIVERGENCE_LOCAL_REF).strip() == local
    assert git(pc_b.vault.path, "rev-parse", DIVERGENCE_REMOTE_REF).strip() == remote
    versions = pc_b.divergent_versions(str(SUBJECT))
    assert versions is not None
    assert versions.local == "name: Física B\nstyle_guide: null\n"
    assert versions.remote == "name: Física A\nstyle_guide: null\n"
    assert pc_b.divergent_versions("vault.yaml") is None
    # The working tree still holds the local side, untouched.
    assert (pc_b.vault.path / SUBJECT).read_text() == "name: Física B\nstyle_guide: null\n"

    # A failed pull for another reason leaves the divergence reported.
    git(pc_b.vault.path, "remote", "set-url", "origin", str(pc_b.vault.path.parent / "gone.git"))
    assert pc_b.sync().outcome == "offline"
    assert pc_b.status().divergence == divergence


def test_a_successful_sync_after_resolving_forgets_the_divergence(
    pc_a: GitSync, pc_b: GitSync
) -> None:
    (pc_a.vault.path / SUBJECT).write_text("name: Física A\nstyle_guide: null\n")
    pc_a.flush()
    (pc_b.vault.path / SUBJECT).write_text("name: Física B\nstyle_guide: null\n")
    pc_b.checkpoint("cambio en B")
    assert pc_b.sync().outcome == "conflict"

    # The student keeps the remote side: B drops its commit (a later UI's job; git here).
    git(pc_b.vault.path, "reset", "--hard", "FETCH_HEAD")
    result = pc_b.sync()

    assert result.ok, result
    assert pc_b.status().divergence is None
    assert not {DIVERGENCE_LOCAL_REF, DIVERGENCE_REMOTE_REF} & refs(pc_b)
    assert pc_b.divergent_versions(str(SUBJECT)) is None
