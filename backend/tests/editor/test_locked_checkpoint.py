"""A turn's write and its checkpoint are one locked step; an undo that changes nothing is refused.

#410: the sync loop commits pending files on its own schedule. A turn that wrote the notes and
then called `checkpoint` could lose its files to a batch commit in between, leaving the turn's
commit -- the one "Deshaz" reverts -- without them. The race is forced here by a "sync loop"
thread that commits right after the write.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from racing_sync import RacingSyncLoop
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import VaultGitSettings
from studentassistant.editor import direct_edit, notes_lock, revise
from studentassistant.editor.direct_edit import save_student_edit
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.revise import (
    EDIT_TOOL,
    NOT_UNDOABLE_WARNING,
    NOTES_UNDONE_KIND,
    RevisionResult,
    UndoConflictError,
    chat_history,
    revise_notes,
    undo_last_revision,
)
from studentassistant.llm import FakeClaude
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    read_conversation,
    read_notes,
)

NOTES_FILE = "apuntes.md"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _files_of(vault: Vault, commit: str) -> str:
    return _git(vault, "show", "--name-only", "--format=", commit)


def _delete_notation(fake: FakeClaude) -> FakeClaude:
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Quito la notación",
            "ops": [{"op": "delete_block", "section": "definicion", "block": 2}],
        },
        text="Hecho.",
    )


def _revise(topic: ReviseTopic, sync: GitSync, fake: FakeClaude) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Quita la notación",
            client=fake.client("editor"),
            sync=sync,
        )
    )


def test_a_sync_commit_racing_a_revision_does_not_take_its_files(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    racer = RacingSyncLoop(sync, revise.write_notes)
    monkeypatch.setattr(revise, "write_notes", racer)

    result = _revise(topic, sync, _delete_notation(FakeClaude()))
    racer.finish()

    assert result.applied and result.commit is not None and result.warning is None
    assert f"notes/{NOTES_FILE}" in _files_of(topic.vault, result.commit)
    assert all(
        commit is None or NOTES_FILE not in _files_of(topic.vault, commit)
        for commit in racer.commits
    )
    # ... so "Deshaz" really undoes it.
    undone = _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert undone.notes_changed and read_notes(topic.vault, topic.subject, topic.topic) == (
        topic.notes
    )


def test_a_sync_commit_racing_a_student_save_does_not_take_its_files(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    racer = RacingSyncLoop(sync, direct_edit.write_notes)
    monkeypatch.setattr(direct_edit, "write_notes", racer)
    edited = topic.notes.replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2]\n\nMío.")

    result = _run(
        save_student_edit(
            topic.vault,
            topic.subject,
            topic.topic,
            edited,
            notes_revision(topic.notes),
            sync=sync,
        )
    )
    racer.finish()

    assert result.commit is not None
    assert f"notes/{NOTES_FILE}" in _files_of(topic.vault, result.commit)
    assert _git(topic.vault, "log", "-1", "--format=%s", "--", f"*{NOTES_FILE}").strip() == (
        f"Apuntes de {topic.subject}/{topic.topic} editados por el estudiante"
    )


def test_a_busy_git_lock_applies_the_turn_and_says_it_cannot_be_undone(
    topic: ReviseTopic, tmp_vault: Vault
) -> None:
    sync = GitSync(tmp_vault, VaultGitSettings(timeout_seconds=0.2))
    held, release = threading.Event(), threading.Event()

    def other_process() -> None:
        with sync.locked():
            held.set()
            release.wait(10)

    holder = threading.Thread(target=other_process)
    holder.start()
    try:
        assert held.wait(5)
        result = _revise(topic, sync, _delete_notation(FakeClaude()))
    finally:
        release.set()
        holder.join(10)

    assert result.applied and result.notes_changed and result.commit is None
    assert result.warning == NOT_UNDOABLE_WARNING and "no se podrá deshacer" in result.warning
    assert read_notes(topic.vault, topic.subject, topic.topic) == result.notes
    assert sync.status().pending_changes  # the sync loop commits it later


def _pre_410_checkpointing(subject: str, topic: str) -> Callable[[GitSync], Any]:
    """The old race, replayed: the batch commit takes the files, the checkpoint only a record."""

    @contextmanager
    def checkpointing(sync: GitSync) -> Iterator[Callable[[str], str | None]]:
        def commit(message: str) -> str | None:
            sync.checkpoint("lote")
            record = ConversationRecord(time=datetime.now(UTC), kind="note", detail={"x": 1})
            append_conversation_record(sync.vault, subject, topic, "editor", record)
            return sync.checkpoint(message)

        yield commit

    return checkpointing


def test_an_undo_that_would_change_nothing_is_refused_and_not_recorded(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(revise, "checkpointing", _pre_410_checkpointing(topic.subject, topic.topic))
    turn = _revise(topic, sync, _delete_notation(FakeClaude()))
    assert turn.commit is not None and NOTES_FILE not in _files_of(topic.vault, turn.commit)
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    sync.checkpoint("conversación")  # the turn's last records, which the undo commits first
    head = _git(topic.vault, "rev-parse", "HEAD")

    with pytest.raises(UndoConflictError, match="no cambiaría nada"):
        _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))

    assert read_notes(topic.vault, topic.subject, topic.topic) == notes
    assert _git(topic.vault, "rev-parse", "HEAD") == head
    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")]
    assert NOTES_UNDONE_KIND not in kinds
    assert not chat_history(topic.vault, topic.subject, topic.topic).turns[-1].undone


def test_checkpointing_holds_the_git_lock_across_the_body(sync: GitSync) -> None:
    sync.checkpoint("fixture")
    with notes_lock.checkpointing(sync) as commit:
        outcome: list[bool] = []

        def other() -> None:
            try:
                with GitSync(sync.vault, VaultGitSettings(timeout_seconds=0.1)).locked():
                    outcome.append(True)
            except Exception:
                outcome.append(False)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        assert outcome == [False]
        assert commit("nada") is None  # nothing pending: never an empty commit
