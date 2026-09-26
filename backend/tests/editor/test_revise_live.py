"""Editor turns on the latest notes (`editor.revise` with `editor.direct_edit`): a student save
during a turn, a document grown from nothing, the student's edits in the editor's history."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.direct_edit import save_student_edit
from studentassistant.editor.notes_format import notes_revision, topic_source_resolver, validate
from studentassistant.editor.revise import (
    EDIT_TOOL,
    MAX_REASKS,
    NOTES_CHANGED_NOTE,
    REPLY_DELTA,
    UndoConflictError,
    revise_notes,
    undo_last_revision,
)
from studentassistant.llm import FakeClaude, LLMRequest
from studentassistant.vault import GitSync, Vault, create_topic, read_notes

STUDENT_LINE = "Lo que añado yo."


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


def _notes(topic: ReviseTopic) -> str:
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    return notes


def _delete_notation(fake: FakeClaude, block: int) -> FakeClaude:
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": "Quito la notación",
            "ops": [{"op": "delete_block", "section": "definicion", "block": block}],
        },
        text="Quito la notación.",
    )


def _texts(request: LLMRequest) -> str:
    return "\n".join(
        block.get("text", "") or str(block.get("content", ""))
        for message in request.messages
        for block in message["content"]
        if isinstance(block, dict)
    )


def _student_edit(topic: ReviseTopic, sync: GitSync) -> str:
    """Save an extra paragraph at the start of #proximo-dia, as the student; the new notes."""
    current = _notes(topic)
    edited = current.replace(
        "## 2. Próximo día {#proximo-dia}\n\n",
        f"## 2. Próximo día {{#proximo-dia}}\n\n{STUDENT_LINE}\n\n",
    )
    result = _run(
        save_student_edit(
            topic.vault, topic.subject, topic.topic, edited, notes_revision(current), sync=sync
        )
    )
    return result.notes


def test_a_student_save_during_a_turn_is_kept_and_the_turn_is_redone_on_it(
    topic: ReviseTopic, sync: GitSync
) -> None:
    # The first answer's ops are numbered against the notes before the save; the re-ask is
    # answered with the same change on the new block map.
    fake = _delete_notation(_delete_notation(FakeClaude(), 2), 2)
    saved: list[str] = []

    async def on_reply(kind: str, data: dict[str, Any]) -> None:
        # While the editor's first answer streams, the student saves the notes.
        if kind == REPLY_DELTA and data["attempt"] == 1 and not saved:
            current = await asyncio.to_thread(read_notes, topic.vault, topic.subject, topic.topic)
            assert current is not None
            edited = current.replace(
                "## 2. Próximo día {#proximo-dia}\n\n",
                f"## 2. Próximo día {{#proximo-dia}}\n\n{STUDENT_LINE}\n\n",
            )
            result = await save_student_edit(
                topic.vault,
                topic.subject,
                topic.topic,
                edited,
                notes_revision(current),
                sync=sync,
            )
            saved.append(result.notes)

    result = _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Quita la notación",
            client=fake.client("editor"),
            sync=sync,
            on_reply=on_reply,
        )
    )

    assert saved, "the student's save ran during the turn"
    assert result.applied and result.attempts == 2
    final = _notes(topic)
    assert f"{STUDENT_LINE}[^est]" in final  # the student's change
    assert "Se escribe $f'(x)$" not in final  # the editor's change
    assert result.revision == notes_revision(final)
    reask = _texts(fake.requests[1])
    assert NOTES_CHANGED_NOTE in reask and STUDENT_LINE in reask
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    assert validate(final, "estricto", resolver) == []


def test_a_turn_whose_notes_keep_changing_applies_nothing(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = FakeClaude()
    for _ in range(MAX_REASKS + 1):
        _delete_notation(fake, 2)
    count = [0]

    async def on_reply(kind: str, data: dict[str, Any]) -> None:
        if kind == REPLY_DELTA and data["attempt"] > count[0]:
            count[0] = data["attempt"]
            current = await asyncio.to_thread(read_notes, topic.vault, topic.subject, topic.topic)
            assert current is not None
            edited = current.replace("[^t3][^p2]", f"[^t3][^p2] (vez {count[0]})")
            await save_student_edit(
                topic.vault,
                topic.subject,
                topic.topic,
                edited,
                notes_revision(current),
                sync=sync,
            )

    result = _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Quita la notación",
            client=fake.client("editor"),
            sync=sync,
            on_reply=on_reply,
        )
    )

    assert not result.applied and result.warning and "mientras" in result.warning
    final = _notes(topic)
    assert f"(vez {MAX_REASKS + 1})" in final and "Se escribe $f'(x)$" in final
    assert result.revision == notes_revision(final)


def test_a_turn_grows_a_document_from_nothing(tmp_vault: Vault, topic: ReviseTopic) -> None:
    sync = GitSync(tmp_vault)
    bare = create_topic(tmp_vault, topic.subject, "Integrales").slug
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        {
            "summary": "Empiezo los apuntes",
            "ops": [
                {
                    "op": "add_section",
                    "level": 2,
                    "title": "1. Idea",
                    "anchor": "idea",
                    "text": "La integral acumula.[^ia]",
                }
            ],
            "footnotes": [
                {"label": "ia", "definition": "Ampliado por la IA: no está en tus fuentes"}
            ],
            "fidelity_mode": "ampliado",
        },
        text="Empiezo los apuntes.",
    )

    result = _run(
        revise_notes(
            tmp_vault, topic.subject, bare, "Empieza", client=fake.client("editor"), sync=sync
        )
    )

    assert result.applied and result.errors == []
    notes = read_notes(tmp_vault, topic.subject, bare)
    assert notes == (
        "# Integrales\n\n## 1. Idea {#idea}\n\nLa integral acumula.[^ia]\n\n"
        "[^ia]: Ampliado por la IA: no está en tus fuentes\n"
    )
    assert result.changed_sections == ["idea"] and result.revision == notes_revision(notes)
    assert "add_section" in _texts(fake.requests[0])


def test_the_editor_is_told_of_the_students_own_edits(topic: ReviseTopic, sync: GitSync) -> None:
    _student_edit(topic, sync)
    fake = FakeClaude().reply_text("Visto.")

    _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "¿Qué he cambiado?",
            client=fake.client("editor"),
            sync=sync,
        )
    )

    text = _texts(fake.requests[0])
    assert "El estudiante editó él mismo los apuntes (#proximo-dia)" in text
    assert f"+{STUDENT_LINE}[^est]" in text


def test_undoing_a_turn_after_a_student_save_is_a_conflict(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        {
            "summary": "Quito la notación",
            "ops": [{"op": "delete_block", "section": "definicion", "block": 2}],
        },
        text="Hecho.",
    )
    _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Quita",
            client=fake.client("editor"),
            sync=sync,
        )
    )
    saved = _student_edit(topic, sync)

    with pytest.raises(UndoConflictError):
        _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert _notes(topic) == saved
