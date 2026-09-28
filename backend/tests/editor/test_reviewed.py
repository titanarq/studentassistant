"""Reviewed state (`editor.reviewed`, #474): `notes.reviewed` records, settled blocks, block map."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from doubts_topic import DoubtsTopic, make_doubts_topic, review_reply
from studentassistant.editor.direct_edit import save_student_edit
from studentassistant.editor.doubts import (
    DECISION_TOOL,
    REVIEW_TOOL,
    DoubtAnswer,
    answer_doubt,
    dismiss_doubt,
    review_doubts,
)
from studentassistant.editor.edits import SETTLED_LEGEND, SETTLED_MARK, describe_sections
from studentassistant.editor.notes_format import notes_revision
from studentassistant.editor.reviewed import (
    NOTES_REVIEWED_RECORD,
    block_key,
    blocks_citing,
    changed_block_keys,
    record_reviewed,
    reviewed_keys,
    settled_blocks,
)
from studentassistant.llm import FakeClaude, LLMRequest
from studentassistant.vault import GitSync, Vault, read_conversation, read_notes

DEFINITION = "**Derivada**: el límite del cociente incremental.[^p1][^t1]"
NOTATION = "Se escribe $f'(x)$.[^t2]"
NEW_NOTATION = "Se escribe $f'(x)$ o también $\\frac{df}{dx}$.[^t2]"
NEXT_DAY = "- La regla de la cadena, que se verá mañana.[^t3][^p2]"


@pytest.fixture
def topic(tmp_vault: Vault) -> DoubtsTopic:
    return make_doubts_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


def _records(topic: DoubtsTopic) -> list[dict[str, Any]]:
    return [
        record.detail or {}
        for record in read_conversation(topic.vault, topic.subject, topic.topic, "editor")
        if record.kind == NOTES_REVIEWED_RECORD
    ]


def _covered(topic: DoubtsTopic) -> list[dict[str, Any]]:
    """The records' reason and blocks, without what each review saw (#483)."""
    return [{"reason": r["reason"], "blocks": r["blocks"]} for r in _records(topic)]


def _save(topic: DoubtsTopic, sync: GitSync, text: str) -> None:
    base = read_notes(topic.vault, topic.subject, topic.topic)
    assert base is not None
    _run(
        save_student_edit(
            topic.vault, topic.subject, topic.topic, text, notes_revision(base), sync=sync
        )
    )


def _last_text(request: LLMRequest) -> str:
    content = request.messages[0]["content"]
    return [b["text"] for b in content if b["type"] == "text"][-1]


# -- keys ---------------------------------------------------------------------------------


def test_a_block_key_ignores_footnote_refs_and_whitespace() -> None:
    key = block_key(NOTATION)
    assert key == block_key("Se escribe   $f'(x)$.[^t9][^p1]")
    assert key == block_key("Se escribe\n$f'(x)$.")
    assert key != block_key(NEW_NOTATION)
    assert len(key) == 64


def test_changed_block_keys_are_the_added_or_changed_blocks(topic: DoubtsTopic) -> None:
    before = topic.notes
    after = before.replace(NOTATION, NEW_NOTATION).replace("[^p1][^t1]", "[^p1]")
    # Dropping a footnote ref changes no key; the rewritten sentence does.
    assert changed_block_keys(before, after) == [block_key(NEW_NOTATION)]
    assert changed_block_keys(before, before) == []
    assert block_key(DEFINITION) in changed_block_keys(None, before)


def test_blocks_citing_follow_the_footnotes_to_their_source_files(topic: DoubtsTopic) -> None:
    notes = topic.notes
    assert blocks_citing(notes, ["sources/notes/page-001.jpg"]) == [block_key(DEFINITION)]
    assert blocks_citing(notes, ["sources/notes/page-002.jpg#x"]) == [block_key(NEXT_DAY)]
    assert set(blocks_citing(notes, [f"sessions/{topic.session}"])) == {
        block_key(DEFINITION),
        block_key(NOTATION),
        block_key(NEXT_DAY),
    }
    assert blocks_citing(notes, []) == [] and blocks_citing(None, ["x"]) == []


def test_records_without_blocks_are_not_written(topic: DoubtsTopic) -> None:
    assert not record_reviewed(topic.vault, topic.subject, topic.topic, "student_edit", [])
    assert _records(topic) == []
    assert reviewed_keys(topic.vault, topic.subject, topic.topic) == set()


# -- records written by what the student does ---------------------------------------------


def test_a_student_save_records_the_blocks_it_added_or_changed(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _save(topic, sync, topic.notes.replace(NOTATION, NEW_NOTATION))

    assert _covered(topic) == [{"reason": "student_edit", "blocks": [block_key(NEW_NOTATION)]}]
    [record] = _records(topic)
    # What the review saw (#483): the sources the block cited, the doubts open then.
    assert record["sources"] == {block_key(NEW_NOTATION): [f"sessions/{topic.session}"]}
    assert record["open_doubts"] == ["p-1", "p-4", "p-5"]
    assert reviewed_keys(topic.vault, topic.subject, topic.topic) == {block_key(NEW_NOTATION)}


def test_a_save_that_changes_nothing_records_nothing(topic: DoubtsTopic, sync: GitSync) -> None:
    _save(topic, sync, topic.notes)
    assert _records(topic) == []


def test_dismissing_a_doubt_reviews_the_blocks_citing_its_sources(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    # p-4 is about page 1 (its capture), cited only by the definition.
    _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync))

    assert _covered(topic) == [{"reason": "doubt_closed", "blocks": [block_key(DEFINITION)]}]
    # Written after the close: the closed doubt is not among those open at the review.
    assert _records(topic)[0]["open_doubts"] == ["p-1", "p-5"]


def test_answering_a_doubt_reviews_the_blocks_citing_its_sources_after_the_edit(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    answered = "**Derivada**: el límite del cociente incremental cuando h tiende a cero.[^p1][^t1]"
    fake = FakeClaude().reply_tool(
        DECISION_TOOL,
        {
            "resolution": "En la página 1 pone «cociente incremental».",
            "edits": [
                {"op": "replace_block", "section": "definicion", "block": 1, "text": answered}
            ],
        },
    )
    result = _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            "p-4",
            DoubtAnswer(answer="incremental"),
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )

    assert result.notes_changed
    assert _covered(topic) == [{"reason": "doubt_closed", "blocks": [block_key(answered)]}]


# -- settled blocks and the block map ------------------------------------------------------


def test_a_reviewed_block_is_settled_only_once_no_open_doubt_names_its_sources(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    vault, subject, name = topic.vault, topic.subject, topic.topic
    _save(topic, sync, topic.notes.replace(NOTATION, NEW_NOTATION))
    notes = read_notes(vault, subject, name)
    # p-1 (open) is about a segment of the session every block cites: nothing settled yet.
    assert settled_blocks(vault, subject, name, notes) == set()

    _run(dismiss_doubt(vault, subject, name, "p-1", sync=sync))

    # Every block citing the session is now reviewed, but p-4 / p-5 still name pages 1 and 2.
    assert reviewed_keys(vault, subject, name) >= {
        block_key(DEFINITION),
        block_key(NEW_NOTATION),
        block_key(NEXT_DAY),
    }
    assert settled_blocks(vault, subject, name, notes) == {block_key(NEW_NOTATION)}
    # A doubt mark or a new wording unsettles the block.
    marked = notes.replace("$f'(x)$ o", "$f'(x)$ [[?o]]")
    assert settled_blocks(vault, subject, name, marked) == set()
    assert settled_blocks(vault, subject, name, None) == set()


def test_the_block_map_marks_settled_blocks(topic: DoubtsTopic) -> None:
    plain = describe_sections(topic.notes)
    assert SETTLED_MARK not in plain and SETTLED_LEGEND not in plain

    marked = describe_sections(topic.notes, {block_key(NOTATION)})

    lines = marked.splitlines()
    assert f"  bloque 2 (paragraph) {SETTLED_MARK}: Se escribe $f'(x)$.[^t2]" in lines
    assert any(line.startswith("  bloque 1 (paragraph): **Derivada**") for line in lines)
    assert lines[-1] == SETTLED_LEGEND


def test_the_doubts_review_shows_the_editor_the_settled_blocks(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    vault, subject, name = topic.vault, topic.subject, topic.topic
    _run(dismiss_doubt(vault, subject, name, "p-1", sync=sync))
    reply = review_reply(topic)
    reply["decisions"] = [d for d in reply["decisions"] if d["pending_id"] != "p-1"]
    fake = FakeClaude().reply_tool(REVIEW_TOOL, reply)

    _run(review_doubts(vault, subject, name, client=fake.client("editor"), sync=sync, host="pc"))

    instruction = _last_text(fake.requests[0])
    assert f"bloque 2 (paragraph) {SETTLED_MARK}: Se escribe" in instruction
    assert f"bloque 1 (paragraph) {SETTLED_MARK}" not in instruction
