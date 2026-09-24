"""Debounced push, flush at session end, offline retries with backoff, the status object."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from git_helpers import ManualClock, commit_count, git

from studentassistant.config import VaultGitSettings
from studentassistant.vault import Vault, create_subject, create_topic
from studentassistant.vault.git import GitResult
from studentassistant.vault.sync import GitSync, _classify

SETTINGS = VaultGitSettings(push_backoff_initial_seconds=10, push_backoff_max_seconds=35)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def sync(tmp_vault: Vault, git_origin: Path, clock: ManualClock) -> GitSync:
    return GitSync(tmp_vault, SETTINGS, clock=clock)


def origin_head(origin: Path) -> str | None:
    heads = git(origin, "for-each-ref", "--format=%(objectname)", "refs/heads/main").strip()
    return heads or None


def test_a_commit_is_pushed_after_the_debounce(
    tmp_vault: Vault, git_origin: Path, sync: GitSync, clock: ManualClock
) -> None:
    head = sync.checkpoint("primer commit")
    assert sync.status().next_push_due == clock.now + 120
    assert sync.status().pending_commits == 1

    clock.advance(60)
    create_subject(tmp_vault, "Física")
    head = sync.checkpoint("materia")  # a later commit does not push the push back
    clock.advance(59)
    sync.run_due()
    assert origin_head(git_origin) is None

    clock.advance(1)
    sync.run_due()
    assert origin_head(git_origin) == head
    status = sync.status()
    assert status.pending_commits == 0
    assert status.next_push_due is None
    assert status.last_push_at is not None


def test_flush_commits_and_pushes_at_once(
    tmp_vault: Vault, git_origin: Path, sync: GitSync
) -> None:
    create_subject(tmp_vault, "Física")
    sync.note_change()

    status = sync.flush()

    assert not status.pending_changes
    assert status.pending_commits == 0
    assert origin_head(git_origin) == git(tmp_vault.path, "rev-parse", "HEAD").strip()


def test_offline_push_is_retried_with_backoff_and_recovers(
    tmp_vault: Vault, git_origin: Path, sync: GitSync, clock: ManualClock
) -> None:
    sync.checkpoint("primer commit")
    sync.flush()
    away = git_origin.with_name("origin-away.git")
    git_origin.rename(away)  # the remote is unreachable

    create_subject(tmp_vault, "Física")
    sync.checkpoint("materia")
    create_topic(tmp_vault, "fisica", "Cinemática")
    local_head = sync.checkpoint("tema")

    status = sync.flush()  # never raises
    assert status.last_push_failure is not None
    assert status.last_push_failure.kind == "offline"
    assert status.consecutive_push_failures == 1
    assert status.pending_commits == 2
    assert status.next_push_due == clock.now + 10

    clock.advance(9)
    sync.run_due()
    assert sync.status().consecutive_push_failures == 1  # not due yet: no attempt

    clock.advance(1)
    sync.run_due()
    assert sync.status().consecutive_push_failures == 2
    assert sync.status().next_push_due == clock.now + 20
    clock.advance(20)
    sync.run_due()
    assert sync.status().next_push_due == clock.now + 35  # 40, capped at the maximum

    # Nothing local was lost while offline.
    assert git(tmp_vault.path, "rev-parse", "HEAD").strip() == local_head
    assert commit_count(tmp_vault.path) == 3

    away.rename(git_origin)
    clock.advance(35)
    sync.run_due()

    status = sync.status()
    assert status.last_push_failure is None
    assert status.consecutive_push_failures == 0
    assert status.pending_commits == 0
    assert origin_head(git_origin) == local_head


def test_failures_are_classified() -> None:
    def failed(stderr: str) -> GitResult:
        return GitResult(args=("push",), returncode=128, stdout="", stderr=stderr)

    assert _classify(failed("fatal: Authentication failed for 'https://github.com/'")) == "auth"
    assert _classify(failed("fatal: could not read Username for 'https://github.com'")) == "auth"
    assert _classify(failed(" ! [rejected] main -> main (fetch first)")) == "rejected"
    assert _classify(failed("fatal: unable to access: Could not resolve host")) == "offline"
    assert _classify(failed("fatal: something else")) == "error"
    timed_out = GitResult(args=("push",), returncode=-1, stdout="", stderr="", timed_out=True)
    assert _classify(timed_out) == "offline"


def test_the_scheduler_loop_runs_git_off_the_event_loop(
    tmp_vault: Vault, git_origin: Path, clock: ManualClock
) -> None:
    sync = GitSync(
        tmp_vault,
        VaultGitSettings(commit_quiet_seconds=0, push_debounce_seconds=0),
        clock=clock,
    )
    create_subject(tmp_vault, "Física")
    sync.note_change()

    async def scenario() -> None:
        task = asyncio.create_task(sync.run(interval=0.01))
        for _ in range(500):
            if origin_head(git_origin) is not None:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(scenario())

    assert origin_head(git_origin) == git(tmp_vault.path, "rev-parse", "HEAD").strip()
