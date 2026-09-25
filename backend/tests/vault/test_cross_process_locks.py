"""Two processes on one vault (the server and the CLI, say) never collide (#165).

Every child is a real `python` process running the vault code, so the only thing keeping them
apart is the `flock` under `.git/`. Every wait is bounded: a child that hangs fails the test.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    GitCommandError,
    GitSync,
    Vault,
    VaultBusyError,
    create_subject,
    create_topic,
    list_sources,
    put_source,
)
from studentassistant.vault.locking import (
    LOCKS_DIRNAME,
    directory_lock,
    git_lock,
)
from studentassistant.vault.sources import sources_directory

CHILD_TIMEOUT_SECONDS = 60
PAGES_PER_WRITER = 15

WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from studentassistant.config import VaultGitSettings
    from studentassistant.vault import GitSync, Vault, put_source

    root, subject, topic, label, count = sys.argv[1:6]
    vault = Vault.open(Path(root))
    sync = GitSync(vault, VaultGitSettings(timeout_seconds=30))
    for number in range(int(count)):
        put_source(vault, subject, topic, "notes", "foto.jpg", f"{label}-{number}".encode(),
                   {"writer": label, "n": number})
        # `None` without an error is fine: the other process's commit already took this page.
        sync.checkpoint(f"{label} {number}")
        if sync.status().last_error is not None:
            print(f"checkpoint failed: {sync.status().last_error}", file=sys.stderr)
            sys.exit(1)
    """
)

HOLDER = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    from studentassistant.vault.locking import git_lock

    root, ready = sys.argv[1:3]
    with git_lock(Path(root)).hold(10):
        Path(ready).write_text("held")
        time.sleep(float(sys.argv[3]))
    """
)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Química").slug
    return subject, create_topic(tmp_vault, subject, "Enlace químico").slug


def _spawn(script: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process: subprocess.Popen[str]) -> None:
    try:
        _out, err = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("a child process did not finish in time")
    assert process.returncode == 0, err


def _wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    """Poll for `path` without sleeping in the test: bounded by the child timeout."""
    done = threading.Event()
    for _ in range(CHILD_TIMEOUT_SECONDS * 50):
        if path.exists():
            return
        if process.poll() is not None:
            pytest.fail(f"the holder exited early: {process.communicate()[1]}")
        done.wait(0.02)
    pytest.fail("the holder never took the lock")


def test_two_processes_writing_one_topic_get_distinct_numbers_and_commit(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, topic_slug = topic
    writers = [
        _spawn(WRITER, str(tmp_vault.path), subject, topic_slug, label, str(PAGES_PER_WRITER))
        for label in ("servidor", "cli")
    ]
    for writer in writers:
        _finish(writer)

    stored = list_sources(tmp_vault, subject, topic_slug)
    names = [Path(source.path).name for source in stored]
    assert len(names) == 2 * PAGES_PER_WRITER
    assert names == [f"page-{number:03d}.jpg" for number in range(1, 2 * PAGES_PER_WRITER + 1)]
    contents = {(tmp_vault.path / source.path).read_bytes() for source in stored}
    assert len(contents) == 2 * PAGES_PER_WRITER  # nobody overwrote anybody
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_vault.path,
        capture_output=True,
        text=True,
        check=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )
    assert status.stdout == ""  # everything committed, and the lock files are not content
    assert (tmp_vault.path / ".git" / LOCKS_DIRNAME).is_dir()


def test_a_git_lock_held_by_another_process_makes_git_wait_then_fail_softly(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    subject, topic_slug = topic
    ready = tmp_path / "ready"
    holder = _spawn(HOLDER, str(tmp_vault.path), str(ready), "3")
    try:
        _wait_for(ready, holder)
        put_source(tmp_vault, subject, topic_slug, "notes", "a.jpg", b"a", {})
        sync = GitSync(tmp_vault, VaultGitSettings(timeout_seconds=0.2))

        assert sync.checkpoint("importar") is None
        assert "otro proceso" in (sync.status().last_error or "")
        assert sync.status().pending_changes is False  # nothing was noted; nothing was lost
        assert sync.sync().outcome == "error"
        assert sync.push_now() is False
        with pytest.raises(GitCommandError, match="otro proceso"):
            sync.list_notes_tags(subject, topic_slug)
    finally:
        _finish(holder)

    # Once the other process is done, the same writer commits normally.
    patient = GitSync(tmp_vault, VaultGitSettings(timeout_seconds=30))
    assert patient.checkpoint("importar") is not None


def test_a_directory_lock_held_elsewhere_bounds_put_source(
    tmp_vault: Vault, topic: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, topic_slug = topic
    monkeypatch.setattr("studentassistant.vault.sources.SOURCE_LOCK_TIMEOUT_SECONDS", 0.1)
    lock = directory_lock(
        tmp_vault.path, sources_directory(tmp_vault, subject, topic_slug, "notes")
    )
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock.hold(5):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        with pytest.raises(VaultBusyError, match="otro proceso"):
            put_source(tmp_vault, subject, topic_slug, "notes", "a.jpg", b"a", {})
    finally:
        release.set()
        holder.join(5)
    assert list_sources(tmp_vault, subject, topic_slug) == []


def test_the_lock_is_reentrant_within_one_thread(tmp_vault: Vault) -> None:
    lock = git_lock(tmp_vault.path)
    with lock.hold(1), lock.hold(1):
        pass
    with lock.hold(1):  # released after the outer hold
        pass
