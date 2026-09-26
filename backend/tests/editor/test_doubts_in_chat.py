"""Doubts go to the chat, never into the document (#325): the `editor_written` validator rule,
the `doubts` of an editor write, live-session events, one doubt asked at a time, answers on the
latest notes and the chat's `doubt` turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from doubts_topic import DoubtsTopic, make_doubts_topic, review_reply
from generate_topic import valid_notes
from studentassistant.editor.direct_edit import save_student_edit
from studentassistant.editor.doubts import (
    DECISION_TOOL,
    PENDING_QUESTION_KIND,
    PENDING_RESOLVED_KIND,
    REVIEW_TOOL,
    DoubtAnswer,
    OpenSessionError,
    answer_doubt,
    ask_in_chat,
    ask_plan,
    dismiss_doubt,
    list_doubts,
    review_doubts,
)
from studentassistant.editor.generate import DOUBTS_TOOL, generate_notes
from studentassistant.editor.notes_format import notes_revision, validate
from studentassistant.editor.revise import EDIT_TOOL, chat_history, revise_notes
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.llm.transport import TextSink
from studentassistant.observer import STATE_OP_EVENT_KIND, load_observer_snapshot
from studentassistant.vault import (
    GitSync,
    Session,
    Vault,
    list_sessions,
    put_source,
    read_notes,
    read_topic_events,
    start_session,
    write_notes,
)

MARKED = "**Derivada**: el límite del cociente [[?incremental]].[^p1][^t1]"
PAGE_1 = "sources/notes/page-001.jpg"
PAGE_2 = "sources/notes/page-002.jpg"


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


def _notes(topic: DoubtsTopic) -> str:
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    return notes


def _live(session: Session) -> Any:
    """A `LiveSink` over an open session handle, as the server's bus publish does it."""

    async def publish(kind: str, origin: Any, payload: dict[str, Any]) -> str:
        await asyncio.to_thread(session.append_event, kind, origin, payload)
        return session.id

    return publish


def _open_session(topic: DoubtsTopic) -> Session:
    session = start_session(topic.vault, topic.subject, topic.topic, "pc", "1.1")
    session.append_event("session.started", "user", {})
    return session


def _events(topic: DoubtsTopic, session_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (event.kind, event.origin, event.payload)
        for sid, event in read_topic_events(topic.vault, topic.subject, topic.topic)
        if sid == session_id
    ]


def _illegible(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "illegible",
        "text": "Palabra dudosa tras «cociente» en la página 1.",
        "question": "¿Qué pone después de «cociente»?",
        "suggestions": ["incremental", "diferencial"],
        "refs": [PAGE_1],
        **extra,
    }


def _contradiction() -> dict[str, Any]:
    return {
        "kind": "contradiction",
        "text": "La página 1 y la página 2 dan años distintos para la regla.",
        "question": "¿Qué año es el correcto?",
        "options": [
            {"source_id": PAGE_1, "says": "1789"},
            {"source_id": PAGE_2, "says": "1791"},
        ],
        "refs": [PAGE_1, PAGE_2],
    }


def _revise(topic: DoubtsTopic, sync: GitSync, fake: FakeClaude, **options: Any) -> Any:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "Aclara la definición",
            client=fake.client("editor"),
            sync=sync,
            host="pc",
            **options,
        )
    )


def _edit(text: str, doubts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "summary": "Aclaro la definición",
        "ops": [{"op": "replace_block", "section": "definicion", "block": 1, "text": text}],
        "doubts": doubts or [],
    }


# -- the validator -----------------------------------------------------------------------------


def test_an_editor_write_with_a_mark_in_a_changed_block_is_rejected_in_spanish(
    topic: DoubtsTopic,
) -> None:
    before = topic.notes
    after = before.replace("**Derivada**: el límite del cociente incremental.[^p1][^t1]", MARKED)

    errors = validate(after, "estricto", editor_written=True, previous=before)

    assert len(errors) == 1
    assert errors[0].startswith("Sección #definicion, bloque 1 (párrafo «**Derivada**")
    assert "marca de duda" in errors[0] and "`doubts`" in errors[0]
    # Without a previous version (a first generation) every block is new.
    assert len(validate(after, "estricto", editor_written=True)) == 1


def test_legacy_marks_stay_until_touched_and_student_saves_are_not_checked(
    topic: DoubtsTopic,
) -> None:
    legacy = topic.notes.replace(
        "**Derivada**: el límite del cociente incremental.[^p1][^t1]", MARKED
    )
    # The editor changes another block: the legacy mark it did not touch passes.
    edited = legacy.replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$ o $y'$.[^t2]")
    assert validate(edited, "estricto", editor_written=True, previous=legacy) == []
    # The student's own save is never subject to it.
    assert validate(legacy, "estricto") == []


def test_a_revision_writing_a_mark_is_sent_back_and_redone_without_it(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = (
        FakeClaude()
        .reply_tool(EDIT_TOOL, _edit(MARKED), text="Lo aclaro.")
        .reply_tool(
            EDIT_TOOL,
            _edit("**Derivada**: el límite del cociente.[^p1][^t1]", [_illegible()]),
            text="Lo aclaro.",
        )
    )

    result = _revise(topic, sync, fake)

    assert result.applied and result.attempts == 2
    reask = str(fake.requests[1].messages[-1]["content"])
    assert "marca de duda" in reask
    assert "[[?" not in _notes(topic)
    assert len(result.doubts) == 1


# -- doubts out of an editor write -------------------------------------------------------------


def test_the_doubts_of_a_turn_go_to_the_live_session_as_pending_items_and_questions(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    session = _open_session(topic)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        _edit("**Derivada**: el límite del cociente.[^p1][^t1]", [_illegible()]),
        text="Lo aclaro.",
    )

    result = _revise(topic, sync, fake, live=_live(session))

    [pending_id] = result.doubts
    events = _events(topic, session.id)
    kinds = [(kind, origin) for kind, origin, _ in events]
    assert kinds[-2:] == [(STATE_OP_EVENT_KIND, "editor"), (PENDING_QUESTION_KIND, "editor")]
    assert events[-2][2]["op"] == "add_pending" and events[-2][2]["source_refs"] == [PAGE_1]
    assert events[-1][2]["in_chat"] is False
    # No review session was started: the unended session took them.
    assert [meta.kind for meta in list_sessions(topic.vault, topic.subject, topic.topic)].count(
        "review"
    ) == 0
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    [doubt] = [d for d in queue.items if d.item.id == pending_id]
    assert doubt.item.is_open and doubt.item.created_by == "editor"
    assert doubt.question is not None and doubt.question.suggestions == [
        "incremental",
        "diferencial",
    ]


def test_without_a_live_sink_an_unended_session_still_refuses_the_doubts(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _open_session(topic)
    with pytest.raises(OpenSessionError):
        _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync))
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _edit("**Derivada**: el límite.[^p1][^t1]", [_illegible()]), text="Vale."
    )
    result = _revise(topic, sync, fake)
    # The change is applied; only the doubts could not be recorded, and it says so.
    assert result.applied and result.doubts == [] and result.warning is not None


def test_without_an_unended_session_the_doubts_go_to_a_review_session(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _edit("**Derivada**: el límite.[^p1][^t1]", [_illegible()]), text="Vale."
    )
    result = _revise(topic, sync, fake)
    [review] = [
        meta
        for meta in list_sessions(topic.vault, topic.subject, topic.topic)
        if meta.kind == "review"
    ]
    assert review.ended_at is not None
    assert [kind for kind, _o, _p in _events(topic, review.id)] == [
        STATE_OP_EVENT_KIND,
        PENDING_QUESTION_KIND,
    ]
    assert len(result.doubts) == 1


def test_a_disagreement_is_the_students_version_plus_a_contradiction_doubt(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(
        EDIT_TOOL,
        _edit(
            "**Derivada**: el límite del cociente incremental (desde 1789).[^p1][^t1]",
            [_contradiction()],
        ),
        text="Pongo el año de tus apuntes y te pregunto.",
    )

    result = _revise(topic, sync, fake)

    notes = _notes(topic)
    assert "1789" in notes and "1791" not in notes
    [pending_id] = result.doubts
    assert pending_id.startswith("contradiccion-")
    # The observer's open p-5 is the same disagreement: the fold merges the editor's into it.
    [doubt] = [
        d
        for d in list_doubts(topic.vault, topic.subject, topic.topic).items
        if pending_id in (d.item.id, *d.item.merged_ids)
    ]
    assert doubt.item.id == "p-5" and doubt.item.kind == "contradiction"
    assert doubt.question is not None and doubt.question.pending_id == pending_id
    assert [(o.source_id, o.says) for o in doubt.question.options] == [
        (PAGE_1, "1789"),
        (PAGE_2, "1791"),
    ]


def test_a_contradiction_doubt_needs_two_sources_and_citable_refs(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    bad = {
        **_contradiction(),
        "options": [{"source_id": "sources/notes/page-009.jpg", "says": "x"}],
    }
    fake = (
        FakeClaude()
        .reply_tool(EDIT_TOOL, _edit("**Derivada**: el límite.[^p1][^t1]", [bad]), text="Vale.")
        .reply_tool(
            EDIT_TOOL,
            _edit("**Derivada**: el límite.[^p1][^t1]", [_contradiction()]),
            text="Vale.",
        )
    )
    result = _revise(topic, sync, fake)
    assert result.attempts == 2 and len(result.doubts) == 1
    reask = str(fake.requests[1].messages[-1]["content"])
    assert "al menos dos fuentes" in reask and "page-009" in reask


# -- one at a time -----------------------------------------------------------------------------


def test_only_one_doubt_is_asked_at_a_time_and_the_next_follows_its_resolution(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _run(
        review_doubts(
            topic.vault,
            topic.subject,
            topic.topic,
            client=FakeClaude().reply_tool(REVIEW_TOOL, review_reply(topic)).client("editor"),
            sync=sync,
            host="pc",
        )
    )
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert plan.asked is None and plan.to_review == [] and plan.to_ask == ["p-4", "p-5"]

    asked = _run(ask_in_chat(topic.vault, topic.subject, topic.topic, "p-4", sync=sync, host="pc"))
    assert asked.question == "¿Qué pone después de «cociente» en la página 1?"
    assert asked.refs == [PAGE_1]
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert plan.asked == "p-4" and plan.to_ask == ["p-5"]

    _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync, host="pc"))
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert plan.asked is None and plan.to_ask == ["p-5"]


def test_items_whose_pages_are_all_set_aside_are_never_asked(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "notes",
        "page.jpg",
        b"\xff\xd8 blank page",
        {"triage": {"status": "set_aside", "reasons": ["blank"]}},
    )
    # Even cited by the notes, a set-aside page's doubt is left out.
    write_notes(
        topic.vault,
        topic.subject,
        topic.topic,
        topic.notes + "[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)\n",
    )
    session = _open_session(topic)
    session.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {
            "op": "add_pending",
            "pending_id": "p-9",
            "kind": "illegible",
            "text": "Nada legible en la página 3.",
            "source_refs": ["sources/notes/page-003.jpg"],
        },
    )
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert "p-9" not in [*plan.to_review, *plan.to_ask]
    assert plan.to_review == ["p-1", "p-4", "p-5"]


def test_nothing_is_asked_without_notes(tmp_vault: Vault) -> None:
    topic = make_doubts_topic(tmp_vault, with_notes=False)
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert plan.asked is None and plan.to_review == [] and plan.to_ask == []


# -- answers on the latest notes ---------------------------------------------------------------


class _SavingFake(FakeClaude):
    """A `FakeClaude` that lets the student save the notes while its first answer is written."""

    def __init__(self, topic: DoubtsTopic, sync: GitSync) -> None:
        super().__init__()
        self.topic, self.sync, self.saved = topic, sync, False

    async def send(self, request: LLMRequest, on_text: TextSink | None = None) -> LLMResponse:
        if not self.saved:
            self.saved = True
            current = _notes(self.topic)
            edited = current.replace(
                "## 2. Próximo día {#proximo-dia}\n\n",
                "## 2. Próximo día {#proximo-dia}\n\nLo que añado yo.\n\n",
            )
            await save_student_edit(
                self.topic.vault,
                self.topic.subject,
                self.topic.topic,
                edited,
                notes_revision(current),
                sync=self.sync,
            )
        return await super().send(request, on_text)


def _decision(block: int) -> dict[str, Any]:
    return {
        "resolution": "Pone «incremental».",
        "edits": [
            {
                "op": "replace_block",
                "section": "proximo-dia",
                "block": block,
                "text": "- La regla de la cadena, que se estudia mañana.[^t3][^p2]",
            }
        ],
    }


def test_an_answer_while_a_student_save_lands_is_redone_on_the_new_notes(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _run(
        review_doubts(
            topic.vault,
            topic.subject,
            topic.topic,
            client=FakeClaude().reply_tool(REVIEW_TOOL, review_reply(topic)).client("editor"),
            sync=sync,
            host="pc",
        )
    )
    fake = _SavingFake(topic, sync)
    # Numbered against the notes before the save (block 1), then on the new block map (block 2).
    fake.reply_tool(DECISION_TOOL, _decision(1)).reply_tool(DECISION_TOOL, _decision(2))

    result = _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            "p-4",
            DoubtAnswer(suggestion=1),
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )

    assert fake.saved and result.attempts == 2 and result.notes_changed
    notes = _notes(topic)
    assert "Lo que añado yo.[^est]" in notes  # the student's save is kept
    assert "- La regla de la cadena, que se estudia mañana.[^t3][^p2]" in notes
    assert result.revision == notes_revision(notes)
    reask = str(fake.requests[1].messages[-1]["content"])
    assert "el estudiante ha cambiado los apuntes" in reask and "Lo que añado yo" in reask


def test_answering_in_the_live_session_keeps_the_fold_in_order(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    session = _open_session(topic)
    live = _live(session)
    fake = FakeClaude().reply_tool(
        EDIT_TOOL, _edit("**Derivada**: el límite.[^p1][^t1]", [_illegible()]), text="Vale."
    )
    [pending_id] = _revise(topic, sync, fake, live=live).doubts
    _run(
        ask_in_chat(
            topic.vault, topic.subject, topic.topic, pending_id, sync=sync, host="pc", live=live
        )
    )
    answer = FakeClaude().reply_tool(
        DECISION_TOOL, {"resolution": "Pone «incremental».", "edits": []}
    )
    result = _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            pending_id,
            DoubtAnswer(suggestion=1),
            client=answer.client("editor"),
            sync=sync,
            host="pc",
            live=live,
        )
    )

    assert result.session_id == session.id and result.status == "resolved"
    kinds = [kind for kind, _o, _p in _events(topic, session.id)]
    assert kinds[-5:] == [
        STATE_OP_EVENT_KIND,  # add_pending
        PENDING_QUESTION_KIND,  # recorded
        PENDING_QUESTION_KIND,  # asked in the chat
        STATE_OP_EVENT_KIND,  # resolve_pending
        PENDING_RESOLVED_KIND,
    ]
    # A fold from scratch (no snapshot) agrees with the incremental one.
    state = load_observer_snapshot(topic.vault, topic.subject, topic.topic).state
    item = state.pending_item(pending_id)
    assert item is not None and item.status == "resolved"
    plan = ask_plan(topic.vault, topic.subject, topic.topic)
    assert plan.asked is None


def test_the_chat_history_has_the_asked_doubt_and_its_outcome(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _run(
        review_doubts(
            topic.vault,
            topic.subject,
            topic.topic,
            client=FakeClaude().reply_tool(REVIEW_TOOL, review_reply(topic)).client("editor"),
            sync=sync,
            host="pc",
        )
    )
    _run(ask_in_chat(topic.vault, topic.subject, topic.topic, "p-4", sync=sync, host="pc"))
    turns = chat_history(topic.vault, topic.subject, topic.topic).turns
    [resolved_line] = [t for t in turns if t.kind == "doubts_resolved"]
    assert resolved_line.pending_ids == ["p-1"] and "He resuelto" in resolved_line.reply
    [doubt] = [t for t in turns if t.kind == "doubt"]
    assert doubt.pending_id == "p-4" and doubt.status == "open"
    assert doubt.suggestions == ["incremental", "diferencial"] and doubt.doubt_refs == [PAGE_1]

    _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            "p-4",
            DoubtAnswer(suggestion=1),
            client=FakeClaude()
            .reply_tool(DECISION_TOOL, {"resolution": "Pone «incremental».", "edits": []})
            .client("editor"),
            sync=sync,
            host="pc",
        )
    )
    [doubt] = [
        t for t in chat_history(topic.vault, topic.subject, topic.topic).turns if t.kind == "doubt"
    ]
    assert doubt.status == "resolved" and doubt.resolution == "Pone «incremental»."
    assert doubt.answer == "incremental"


# -- generation --------------------------------------------------------------------------------


def test_a_generation_reports_its_doubts_and_writes_no_marks(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    marked = valid_notes(topic.session).replace(
        "**Derivada**: el límite del cociente incremental.[^p1][^t1]", MARKED
    )
    fake = (
        FakeClaude()
        .reply_text(marked)
        .reply_tool(DOUBTS_TOOL, {"doubts": [_illegible()]}, text=valid_notes(topic.session))
    )

    result = _run(
        generate_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            detect_contradictions=False,
            host="pc",
        )
    )

    assert not result.draft and result.attempts == 2 and len(result.doubts) == 1
    assert "marca de duda" in str(fake.requests[1].messages[-1]["content"])
    assert "[[?" not in _notes(topic)
    [doubt] = [
        d
        for d in list_doubts(topic.vault, topic.subject, topic.topic).items
        if d.item.id == result.doubts[0]
    ]
    assert doubt.item.created_by == "editor" and doubt.question is not None
