"""Doubts resolution (`editor.doubts`): auto-resolve with evidence, ask the rest, apply answers."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Awaitable
from typing import Any

import pytest
import yaml

from doubts_topic import DoubtsTopic, make_doubts_topic, review_reply, transcript_id
from studentassistant.editor.doubts import (
    DECISION_TOOL,
    PENDING_QUESTION_KIND,
    PENDING_RESOLVED_KIND,
    REVIEW_TOOL,
    DoubtAnswer,
    DoubtClosedError,
    InvalidAnswerError,
    NotesMissingError,
    OpenSessionError,
    UnknownDoubtError,
    answer_doubt,
    dismiss_doubt,
    list_doubts,
    review_doubts,
)
from studentassistant.llm import FakeClaude, LLMRequest, load_prompt
from studentassistant.observer import STATE_OP_EVENT_KIND, fold
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sessions,
    pending_review_path,
    read_conversation,
    read_notes,
    read_topic_events,
    start_session,
)


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


def _review(topic: DoubtsTopic, sync: GitSync, fake: FakeClaude, **options: Any) -> Any:
    return _run(
        review_doubts(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor"),
            sync=sync,
            host="pc",
            **options,
        )
    )


def _answer(
    topic: DoubtsTopic, sync: GitSync, fake: FakeClaude, pending_id: str, **answer: Any
) -> Any:
    return _run(
        answer_doubt(
            topic.vault,
            topic.subject,
            topic.topic,
            pending_id,
            DoubtAnswer(**answer),
            client=fake.client("editor"),
            sync=sync,
            host="pc",
        )
    )


def _last_text(request: LLMRequest) -> str:
    content = request.messages[0]["content"]
    return [b["text"] for b in content if b["type"] == "text"][-1]


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def _reviewed(topic: DoubtsTopic, sync: GitSync) -> None:
    _review(topic, sync, FakeClaude().reply_tool(REVIEW_TOOL, review_reply(topic)))


# -- review -----------------------------------------------------------------------


def test_review_auto_resolves_with_evidence_and_asks_the_rest(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(REVIEW_TOOL, review_reply(topic))

    result = _review(topic, sync, fake)

    assert result.auto_resolved == ["p-1"] and result.asked == ["p-4", "p-5"]
    assert result.notes_changed and result.attempts == 1 and result.warning is None
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert "- La regla de la cadena, que se repasa mañana.[^t3][^p2]\n" in notes
    assert notes == topic.notes.replace(
        "- La regla de la cadena, que se verá mañana.",
        "- La regla de la cadena, que se repasa mañana.",
    )

    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    assert queue.open_count == 2 and queue.current == "p-4"
    by_id = {d.item.id: d for d in queue.items}
    assert [d.item.id for d in queue.items][:2] == ["p-4", "p-5"]
    closed = by_id["p-1"]
    assert closed.item.status == "auto_resolved" and closed.item.created_by == "observer"
    assert closed.outcome is not None and closed.outcome.evidence[0].source_id == transcript_id(
        topic
    )
    assert by_id["p-4"].question is not None
    assert by_id["p-4"].question.suggestions == ["incremental", "diferencial"]
    assert [o.says for o in by_id["p-5"].question.options] == ["1789", "1791"]

    # One review session, after the study sessions, holds the close and the questions.
    sessions = list_sessions(topic.vault, topic.subject, topic.topic)
    assert sessions[-1].id == result.session_id and sessions[-1].ended_at is not None
    review = [
        (e.kind, e.origin)
        for sid, e in read_topic_events(topic.vault, topic.subject, topic.topic)
        if sid == result.session_id
    ]
    assert review == [
        (STATE_OP_EVENT_KIND, "editor"),
        (PENDING_RESOLVED_KIND, "editor"),
        (PENDING_QUESTION_KIND, "editor"),
        (PENDING_QUESTION_KIND, "editor"),
    ]
    pending = yaml.safe_load(
        pending_review_path(topic.vault, topic.subject, topic.topic).read_text()
    )
    assert pending["open_count"] == 2
    assert "revisadas: 1 resueltas con fuentes, 2 preguntas" in _git(
        topic.vault, "log", "-1", "--format=%s"
    )
    # Only the conversation's last record is left for the sync loop to commit.
    assert _git(topic.vault, "status", "--porcelain").strip().endswith("conversations/editor.jsonl")

    [request] = fake.requests
    assert request.role == "editor"
    assert request.system[0]["text"] == load_prompt("editor_doubts").content
    assert [tool["name"] for tool in request.tools] == [REVIEW_TOOL]
    instruction = _last_text(request)
    assert "- p-1 [unexplained_concept]" in instruction and "p-2" not in instruction
    assert "#proximo-dia" in instruction and "bloque 1 (list)" in instruction
    records = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")]
    assert records == ["context", "user", "assistant", "validation", "pending.reviewed"]


def test_an_auto_resolution_without_evidence_is_sent_back(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    bad = review_reply(topic)
    bad["decisions"][0]["evidence"] = []
    fake = FakeClaude().reply_tool(REVIEW_TOOL, bad).reply_tool(REVIEW_TOOL, review_reply(topic))

    result = _review(topic, sync, fake)

    assert result.attempts == 2 and result.auto_resolved == ["p-1"]
    reask = fake.requests[1].messages[-1]["content"]
    assert reask[0]["type"] == "tool_result" and reask[0]["is_error"] is True
    assert "al menos una fuente" in reask[0]["content"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d[0]["evidence"][0].update(source_id="sources/book/page-009.jpg"), "catálogo"),
        (lambda d: d[1].update(suggestions=[]), "respuestas sugeridas"),
        (lambda d: d[1].update(suggestions=["a", "b", "c", "d"]), "como mucho 3"),
        (lambda d: d[2].update(options=[]), "contradicción"),
        (lambda d: d.pop(), "p-5: falta su decisión"),
        (lambda d: d[0]["edits"][0].update(text="Sin cita."), "no tiene nota al pie"),
        (lambda d: d[0]["edits"][0].update(section="nada"), "#nada"),
    ],
)
def test_a_review_that_fails_the_checks_ends_as_questions(
    topic: DoubtsTopic, sync: GitSync, change: Any, expected: str
) -> None:
    bad = review_reply(topic)
    change(bad["decisions"])
    fake = FakeClaude()
    for _ in range(3):
        fake.reply_tool(REVIEW_TOOL, bad)

    result = _review(topic, sync, fake)

    assert len(fake.requests) == 3 and result.attempts == 3
    assert expected in fake.requests[1].messages[-1]["content"][0]["content"]
    assert result.auto_resolved == [] and result.asked == ["p-1", "p-4", "p-5"]
    assert not result.notes_changed and result.warning
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    assert queue.open_count == 3
    assert all(d.question is not None for d in queue.items if d.item.is_open)


def test_nothing_is_sent_without_open_doubts(tmp_vault: Vault, sync: GitSync) -> None:
    topic = make_doubts_topic(tmp_vault)
    _reviewed(topic, sync)
    for pending_id in ("p-4", "p-5"):
        _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, pending_id, sync=sync))
    fake = FakeClaude()

    result = _review(topic, sync, fake)

    assert fake.requests == [] and result.auto_resolved == [] and result.asked == []


def test_the_review_needs_the_notes(tmp_vault: Vault, sync: GitSync) -> None:
    topic = make_doubts_topic(tmp_vault, with_notes=False)
    fake = FakeClaude()
    with pytest.raises(NotesMissingError):
        _review(topic, sync, fake)
    assert fake.requests == []


def test_nothing_happens_while_the_topic_has_an_unended_session(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    start_session(topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1")
    fake = FakeClaude()
    with pytest.raises(OpenSessionError):
        _review(topic, sync, fake)
    with pytest.raises(OpenSessionError):
        _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync))
    assert fake.requests == []


# -- answers -----------------------------------------------------------------------


def test_answering_with_a_suggestion_applies_the_decision(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _reviewed(topic, sync)
    fake = FakeClaude().reply_tool(
        DECISION_TOOL,
        {
            "resolution": "En la página 1 pone «cociente incremental».",
            "edits": [
                {
                    "op": "replace_block",
                    "section": "definicion",
                    "block": 1,
                    "text": "**Derivada**: el límite del cociente incremental cuando h tiende a"
                    " cero.[^p1][^t1]",
                }
            ],
        },
    )

    result = _answer(topic, sync, fake, "p-4", suggestion=1)

    assert result.status == "resolved" and result.notes_changed and result.warning is None
    assert result.resolution == "En la página 1 pone «cociente incremental»."
    assert "cuando h tiende a cero" in read_notes(topic.vault, topic.subject, topic.topic)
    instruction = _last_text(fake.requests[0])
    assert "Decisión del estudiante: incremental" in instruction
    assert "¿Qué pone después de «cociente» en la página 1?" in instruction
    queue = list_doubts(topic.vault, topic.subject, topic.topic)
    answered = next(d for d in queue.items if d.item.id == "p-4")
    assert answered.item.status == "resolved" and answered.item.resolution == result.resolution
    assert answered.outcome is not None and answered.outcome.suggestion == "incremental"
    assert queue.current == "p-5" and queue.open_count == 1
    assert "Duda resuelta" in _git(topic.vault, "log", "-1", "--format=%s")


def test_a_contradiction_picks_a_source_and_can_keep_the_discarded_version(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    _reviewed(topic, sync)
    fake = FakeClaude().reply_tool(
        DECISION_TOOL,
        {
            "resolution": "Vale 1789, como dice la página 1.",
            "edits": [
                {
                    "op": "insert_after",
                    "section": "definicion",
                    "block": 2,
                    "text": "Fue en 1789.[^p1] En tus apuntes pone 1791.[^p2]",
                }
            ],
        },
    )

    result = _answer(
        topic, sync, fake, "p-5", source_id="sources/notes/page-001.jpg", keep_discarded=True
    )

    assert result.notes_changed
    assert "En tus apuntes pone 1791.[^p2]" in read_notes(topic.vault, topic.subject, topic.topic)
    instruction = _last_text(fake.requests[0])
    assert "Conserva la versión descartada" in instruction
    assert "sources/notes/page-002.jpg dice «1791»" in instruction
    outcome = next(
        d.outcome
        for d in list_doubts(topic.vault, topic.subject, topic.topic).items
        if d.item.id == "p-5"
    )
    assert outcome is not None and outcome.chosen_source is not None
    assert outcome.chosen_source.says == "1789" and [o.says for o in outcome.discarded] == ["1791"]
    assert outcome.keep_discarded


@pytest.mark.parametrize(
    "answer",
    [
        {"suggestion": 3},
        {"source_id": "sources/notes/page-009.jpg"},
        {"keep_discarded": True, "answer": "x"},
        {},
        {"answer": "   "},
    ],
)
def test_an_answer_that_does_not_fit_the_question_is_refused(
    topic: DoubtsTopic, sync: GitSync, answer: dict[str, Any]
) -> None:
    _reviewed(topic, sync)
    fake = FakeClaude()
    pending_id = "p-5" if "source_id" in answer else "p-4"
    with pytest.raises(InvalidAnswerError):
        _answer(topic, sync, fake, pending_id, **answer)
    assert fake.requests == []


def test_a_free_answer_without_notes_is_recorded_without_a_call(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic = make_doubts_topic(tmp_vault, with_notes=False)
    fake = FakeClaude()

    result = _answer(topic, sync, fake, "p-1", answer="Es la derivada de la función compuesta.")

    assert fake.requests == [] and not result.notes_changed
    assert result.resolution == "Es la derivada de la función compuesta."
    state = fold(read_topic_events(topic.vault, topic.subject, topic.topic))
    assert state.pending["p-1"].status == "resolved"


def test_a_decision_the_editor_cannot_apply_is_kept_with_a_warning(
    topic: DoubtsTopic, sync: GitSync
) -> None:
    fake = FakeClaude()
    bad = {"resolution": "x", "edits": [{"op": "delete_block", "section": "nada", "block": 1}]}
    for _ in range(3):
        fake.reply_tool(DECISION_TOOL, bad)

    result = _answer(topic, sync, fake, "p-1", answer="Se explica el martes.")

    assert len(fake.requests) == 3 and result.attempts == 3
    assert result.status == "resolved" and not result.notes_changed and result.warning
    assert result.resolution == "Se explica el martes."
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes


# -- dismiss and errors -----------------------------------------------------------------------


def test_dismissing_closes_the_doubt_without_a_call(topic: DoubtsTopic, sync: GitSync) -> None:
    result = _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync))

    assert result.status == "dismissed" and not result.notes_changed
    state = fold(read_topic_events(topic.vault, topic.subject, topic.topic))
    assert state.pending["p-4"].status == "dismissed"
    events = [e for _, e in read_topic_events(topic.vault, topic.subject, topic.topic)]
    assert events[-1].kind == PENDING_RESOLVED_KIND and events[-1].origin == "user"
    assert json.loads(json.dumps(events[-1].payload)) == {
        "pending_id": "p-4",
        "status": "dismissed",
        "evidence": [],
        "discarded": [],
        "keep_discarded": False,
        "notes_changed": False,
    }
    with pytest.raises(DoubtClosedError):
        _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-4", sync=sync))
    with pytest.raises(UnknownDoubtError):
        _run(dismiss_doubt(topic.vault, topic.subject, topic.topic, "p-99", sync=sync))
