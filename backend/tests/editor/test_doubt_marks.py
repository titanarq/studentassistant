"""Where the open doubts are marked in the notes viewer (#516, `editor.doubt_marks`).

The fixture topic "Derivadas" (`make_revise_topic`): its notes cite page 1 and two transcript
spans in #definicion, page 2 and the span 01:02:05-01:02:10 in #proximo-dia; the observer's open
p-1 is about segment s-3 (01:02:05-01:02:10), assigned to no section.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from generate_topic import valid_notes
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.doubt_marks import MarkedBlock, doubt_marks
from studentassistant.editor.doubts import (
    EditorDoubt,
    ask_in_chat,
    doubt_chat_turns,
    raise_doubts,
)
from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.vault import (
    GitSync,
    Vault,
    end_session,
    put_source,
    read_notes,
    start_session,
    write_notes,
)

PAGE_1 = "sources/notes/page-001.jpg"
BOOK = "sources/book/page-001.jpg"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    return asyncio.run(asyncio.wait_for(coroutine, timeout=10))


def _raise(topic: ReviseTopic, sync: GitSync, *doubts: dict[str, Any]) -> list[str]:
    items = [EditorDoubt.model_validate(d) for d in doubts]
    return _run(raise_doubts(topic.vault, topic.subject, topic.topic, items, sync=sync))


def _doubt(text: str, refs: list[str]) -> dict[str, Any]:
    return {
        "kind": "illegible",
        "text": text,
        "question": f"¿{text}?",
        "suggestions": ["sí", "no"],
        "refs": refs,
    }


def _observer_pending(topic: ReviseTopic, **fields: Any) -> None:
    """An observer `add_pending` in a review session of the topic."""
    session = start_session(topic.vault, topic.subject, topic.topic, "pc", "1.1", kind="review")
    session.append_event(STATE_OP_EVENT_KIND, "observer", {"op": "add_pending", **fields})
    end_session(session)


def _marks(topic: ReviseTopic) -> dict[str, Any]:
    marks = doubt_marks(topic.vault, topic.subject, topic.topic)
    assert marks.count == len(marks.marks)
    return {mark.pending_id: mark for mark in marks.marks}


def test_a_segment_doubt_goes_on_the_block_citing_its_span_only(topic: ReviseTopic) -> None:
    marks = _marks(topic)
    assert list(marks) == ["p-1"]
    mark = marks["p-1"]
    assert mark.level == "block" and mark.kind == "unexplained_concept"
    assert mark.text == "Se menciona la regla de la cadena sin explicarla."
    # #definicion cites the same session, but other stretches of it.
    assert mark.blocks == [MarkedBlock(section="proximo-dia", number=1)]
    assert mark.asked is False


def test_a_page_doubt_goes_on_every_block_citing_the_page_and_the_notes_are_untouched(
    topic: ReviseTopic, sync: GitSync
) -> None:
    before = read_notes(topic.vault, topic.subject, topic.topic)
    [page] = _raise(topic, sync, _doubt("Palabra dudosa en la página 1", [PAGE_1]))
    mark = _marks(topic)[page]
    assert mark.level == "block"
    assert mark.blocks == [MarkedBlock(section="definicion", number=1)]
    assert read_notes(topic.vault, topic.subject, topic.topic) == before


def test_a_doubt_without_refs_goes_in_the_top_header_first(
    topic: ReviseTopic, sync: GitSync
) -> None:
    [whole] = _raise(topic, sync, _doubt("Falta la conclusión del tema", []))
    marks = doubt_marks(topic.vault, topic.subject, topic.topic)
    assert [m.pending_id for m in marks.marks] == [whole, "p-1"]
    top = marks.marks[0]
    assert top.level == "top" and top.blocks == [] and top.section is None


def test_a_segment_no_block_cites_goes_on_its_sections_heading(topic: ReviseTopic) -> None:
    # #definicion's second block cites page 1 instead of 00:00:10-00:00:15 (segment s-2).
    notes = (
        valid_notes(topic.session)
        .replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^p1]")
        .replace(
            f"[^t2]: [Transcripción, 00:00:10–00:00:15](../sessions/{topic.session}"
            "/transcript.jsonl#t=00:00:10-00:00:15)\n",
            "",
        )
    )
    write_notes(topic.vault, topic.subject, topic.topic, notes)
    _observer_pending(
        topic, pending_id="p-9", kind="incomplete", text="No se oye el final.", segment_ids=["s-2"]
    )
    mark = _marks(topic)["p-9"]
    # s-2 is assigned to the observer's «Definición», the heading «1. Definición {#definicion}».
    assert mark.level == "section" and mark.section == "definicion" and mark.blocks == []


def test_irrelevant_and_set_aside_doubts_are_not_marked(topic: ReviseTopic, sync: GitSync) -> None:
    # The book page is not cited by the notes: its doubt is not about them (yet).
    _raise(topic, sync, _doubt("Una cifra del libro", [BOOK]))
    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "book",
        "page.jpg",
        b"\xff\xd8 blank",
        {"triage": {"status": "set_aside", "reasons": ["blank"]}},
    )
    _raise(topic, sync, _doubt("Página en blanco", ["sources/book/page-002.jpg"]))
    assert list(_marks(topic)) == ["p-1"]


def test_a_doubt_asked_in_the_chat_is_flagged_and_shown_again_is_one_turn(
    topic: ReviseTopic, sync: GitSync
) -> None:
    [page] = _raise(topic, sync, _doubt("Palabra dudosa en la página 1", [PAGE_1]))
    for pending_id in (page, "p-1", page):
        _run(ask_in_chat(topic.vault, topic.subject, topic.topic, pending_id, sync=sync))
    marks = _marks(topic)
    assert marks[page].asked and marks["p-1"].asked
    turns = doubt_chat_turns(topic.vault, topic.subject, topic.topic)
    # Shown again: one turn, at its latest asking, with its explanation.
    assert [turn.pending_id for turn in turns] == ["p-1", page]
    assert turns[1].text == "Palabra dudosa en la página 1"


def test_without_notes_nothing_is_marked(topic: ReviseTopic) -> None:
    write_notes(topic.vault, topic.subject, topic.topic, "")
    marks = doubt_marks(topic.vault, topic.subject, topic.topic)
    assert marks.count == 0 and marks.marks == []
