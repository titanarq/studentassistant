"""The voice tutor (`editor.tutor`): the input, the answer and its refs, the conversation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.notes_format import parse
from studentassistant.editor.revise import InvalidMessageError, NotesMissingError, chat_history
from studentassistant.editor.tutor import (
    MAX_QUESTION_CHARS,
    ask_tutor,
    cited_refs,
    tutor_history,
)
from studentassistant.llm import FakeClaude, LLMRequest, RefusalError, load_prompt
from studentassistant.vault import GitSync, Vault, create_topic, read_conversation, read_notes


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))


def _ask(topic: ReviseTopic, fake: FakeClaude, question: str, **kwargs: Any) -> Any:
    return _run(
        ask_tutor(
            topic.vault,
            topic.subject,
            topic.topic,
            question,
            client=fake.client("editor"),
            **kwargs,
        )
    )


def _texts(request: LLMRequest) -> str:
    content = request.messages[0]["content"]
    return "\n".join(b["text"] for b in content if b["type"] == "text")


def test_answers_from_the_notes_and_sources_with_refs(topic: ReviseTopic) -> None:
    reply = (
        "La derivada es el límite del cociente incremental.[^p1][^t1] El libro lo dice igual."
        "[^zz] Y se escribe f prima de x.[^t2][^p1]"
    )
    fake = FakeClaude().reply_text(reply)
    replies = Sink()
    answer = _ask(topic, fake, "  ¿qué era\n la derivada? ", on_reply=replies)

    assert answer.question == "¿qué era la derivada?"
    assert answer.reply == reply
    # Cited labels the notes define, in order of first citation; the unknown one is dropped.
    assert [(r.label, r.kind) for r in answer.refs] == [
        ("p1", "notes"),
        ("t1", "transcript"),
        ("t2", "transcript"),
    ]
    assert answer.refs[0].text == "Apuntes, página 1"
    assert answer.refs[0].source_id == "sources/notes/page-001.jpg"
    assert answer.warning is None
    assert "".join(p["text"] for k, p in replies.items if k == "reply.delta") == reply

    [request] = fake.requests
    assert request.system[0]["text"] == load_prompt("editor_tutor").content
    assert request.tools in (None, [])
    text = _texts(request)
    # The whole topic: the catalogue, the book page, the current notes, then the question.
    assert "Catálogo de fuentes citables" in text
    assert "La derivada de f en a es el límite" in text  # the textbook transcription
    assert "**Derivada**: el límite del cociente incremental.[^p1][^t1]" in text
    assert "Tarea: responder como tutor" in text
    assert "(Es la primera pregunta.)" in text
    assert text.rstrip().endswith("¿qué era la derivada?")
    # Nothing of the notes changed.
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes


def test_conversation_is_its_own_and_feeds_follow_ups(topic: ReviseTopic) -> None:
    times = iter(datetime(2026, 9, 25, 10, minute, tzinfo=UTC) for minute in range(60))
    fake = FakeClaude().reply_text("Es un límite.[^p1]").reply_text("Porque lo dijiste.[^t1]")
    _ask(topic, fake, "¿Qué es la derivada?", clock=lambda: next(times))
    _ask(topic, fake, "¿Y eso por qué?", clock=lambda: next(times))

    second = _texts(fake.requests[1])
    assert "Estudiante: ¿Qué es la derivada?\nTutor: Es un límite.[^p1]" in second

    history = tutor_history(topic.vault, topic.subject, topic.topic)
    assert [(t.question, t.reply) for t in history.turns] == [
        ("¿Qué es la derivada?", "Es un límite.[^p1]"),
        ("¿Y eso por qué?", "Porque lo dijiste.[^t1]"),
    ]
    assert [r.label for r in history.turns[1].refs] == ["t1"]

    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "tutor")]
    assert kinds == ["context", "user", "assistant", "tutor.answer"] * 2
    context = read_conversation(topic.vault, topic.subject, topic.topic, "tutor")[0]
    assert context.detail is not None and context.detail["reason"] == "tutor"
    # The editor chat does not see the tutor's turns.
    assert chat_history(topic.vault, topic.subject, topic.topic).turns == []


def test_sync_is_told_and_warnings(topic: ReviseTopic) -> None:
    class Sync:
        changes = 0

        def note_change(self) -> None:
            self.changes += 1

    sync = Sync()
    fake = FakeClaude().reply_text("  ").reply_text("Cortada", stop_reason="max_tokens")
    empty = _ask(topic, fake, "¿Qué?", sync=sync)  # type: ignore[arg-type]
    assert empty.warning is not None and "no ha contestado" in empty.warning
    cut = _ask(topic, fake, "¿Qué?")
    assert cut.warning is not None and "cortada" in cut.warning
    assert sync.changes == 1


def test_invalid_questions_missing_notes_and_refusal(tmp_vault: Vault, topic: ReviseTopic) -> None:
    fake = FakeClaude()
    with pytest.raises(InvalidMessageError):
        _ask(topic, fake, "   ")
    with pytest.raises(InvalidMessageError):
        _ask(topic, fake, "x" * (MAX_QUESTION_CHARS + 1))
    assert fake.requests == []

    empty = create_topic(tmp_vault, topic.subject, "Integrales").slug
    GitSync(tmp_vault).checkpoint("empty topic")
    with pytest.raises(NotesMissingError):
        _run(ask_tutor(tmp_vault, topic.subject, empty, "¿Qué?", client=fake.client("editor")))

    refusing = FakeClaude().reply_text("No.", stop_reason="refusal")
    with pytest.raises(RefusalError):
        _ask(topic, refusing, "¿Qué?")
    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "tutor")]
    assert kinds == ["context", "user", "assistant"]
    assert tutor_history(topic.vault, topic.subject, topic.topic).turns == []


def test_cited_refs_skips_repeats_and_definitions(topic: ReviseTopic) -> None:
    document = parse(topic.notes)
    refs = cited_refs(document, "a[^p2] b[^p2] c[^p1]\n[^p1]: no es cita")
    assert [r.label for r in refs] == ["p2", "p1"]
    assert cited_refs(document, "sin notas") == []
