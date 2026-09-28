"""App feedback from the chats (`editor.feedback`, #472): the tool call in both chats, the inbox."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.crop import CROP_TOOL
from studentassistant.editor.feedback import FEEDBACK_TOOL, NOT_RECORDED_WARNING
from studentassistant.editor.revise import (
    EDIT_TOOL,
    NO_CHANGE_WARNING,
    RevisionResult,
    chat_history,
    revise_notes,
)
from studentassistant.editor.tutor import TutorAnswer, ask_tutor, tutor_history
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.vault import GitSync, Vault, list_feedback, read_notes


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


def _revise(
    topic: ReviseTopic, sync: GitSync, fake: FakeClaude, message: str, **kwargs: Any
) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            message,
            client=fake.client("editor"),
            sync=sync,
            **kwargs,
        )
    )


def _ask(topic: ReviseTopic, fake: FakeClaude, question: str) -> TutorAnswer:
    return _run(
        ask_tutor(
            topic.vault,
            topic.subject,
            topic.topic,
            question,
            client=fake.client("editor"),
            style="written",
        )
    )


def _texts(request: LLMRequest) -> str:
    return "\n".join(
        block["text"]
        for message in request.messages
        for block in message["content"]
        if block.get("type") == "text"
    )


MEJORA = {
    "kind": "mejora",
    "title": "Exportar los apuntes a PDF",
    "body": "«Apunta una mejora: poder exportar a PDF». Pide exportar los apuntes a PDF.",
}
BUG = {
    "kind": "bug",
    "title": "La foto no se guarda",
    "body": "«Esto es un bug: la foto no se guarda». Al capturar, la foto se pierde.",
}


# -- the workspace chat (Construir) ---------------------------------------------------------------


def test_the_workspace_chat_records_a_mejora_and_leaves_the_notes(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, MEJORA)
    before = read_notes(topic.vault, topic.subject, topic.topic)

    result = _revise(topic, sync, fake, "Apunta una mejora: poder exportar a PDF")

    assert result.feedback is not None
    assert (result.feedback.id, result.feedback.kind) == ("fb-1", "mejora")
    assert result.feedback.title == "Exportar los apuntes a PDF"
    assert result.reply == "He apuntado la mejora: Exportar los apuntes a PDF."
    assert not result.applied and result.warning is None and result.attempts == 1
    assert read_notes(topic.vault, topic.subject, topic.topic) == before

    [item] = list_feedback(topic.vault)
    assert (item.id, item.kind, item.status, item.body) == (
        "fb-1",
        "mejora",
        "nuevo",
        MEJORA["body"],
    )
    assert item.context.subject == topic.subject and item.context.topic == topic.topic
    assert (item.context.route, item.context.mode) == ("workspace", "construir")
    assert item.context.excerpt.endswith("Estudiante: Apunta una mejora: poder exportar a PDF")

    # The editor was offered the strict tool and told when to call it.
    [request] = fake.requests
    assert [tool["name"] for tool in request.tools] == [EDIT_TOOL, CROP_TOOL, FEEDBACK_TOOL]
    assert request.tools[2]["strict"] is True
    assert "report_feedback" in _texts(request)

    # The chat history keeps the item, and the next turn is told it was recorded.
    [turn] = chat_history(topic.vault, topic.subject, topic.topic).turns
    assert turn.feedback == result.feedback
    fake.reply_text("De nada.")
    _revise(topic, sync, fake, "Gracias")
    assert "[Apuntado como comentario sobre la aplicación: Exportar los apuntes a PDF]" in (
        _texts(fake.requests[1])
    )


def test_a_feedback_turn_never_edits_even_with_an_edit_call(
    topic: ReviseTopic, sync: GitSync
) -> None:
    edit = {"summary": "Borro", "ops": [{"op": "delete_section", "section": "definicion"}]}
    fake = FakeClaude().reply(
        LLMResponse(
            model="",
            stop_reason="tool_use",
            content=[
                {"type": "text", "text": "He apuntado el bug."},
                {"type": "tool_use", "id": "t1", "name": FEEDBACK_TOOL, "input": BUG},
                {"type": "tool_use", "id": "t2", "name": EDIT_TOOL, "input": edit},
            ],
        )
    )
    before = read_notes(topic.vault, topic.subject, topic.topic)

    result = _revise(topic, sync, fake, "Esto es un bug: la foto no se guarda")

    assert result.feedback is not None and result.feedback.kind == "bug"
    assert result.reply == "He apuntado el bug."
    assert not result.applied and result.errors == []
    assert read_notes(topic.vault, topic.subject, topic.topic) == before
    assert len(fake.requests) == 1


def test_an_edit_classified_feedback_turn_is_not_re_asked(
    topic: ReviseTopic, sync: GitSync
) -> None:
    """A spoken request the classifier called `edit` that the editor recognised as app feedback."""
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, BUG, text="He apuntado el bug: la foto.")

    result = _revise(topic, sync, fake, "Esto es un bug: la foto no se guarda", expects_change=True)

    assert len(fake.requests) == 1 and result.attempts == 1
    assert result.warning is None and result.warning != NO_CHANGE_WARNING
    assert result.reply == "He apuntado el bug: la foto."
    assert [item.kind for item in list_feedback(topic.vault)] == ["bug"]


def test_a_malformed_feedback_call_records_nothing_and_warns(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, {"kind": "idea", "title": "", "body": "x"})

    result = _revise(topic, sync, fake, "Apunta una mejora: algo", expects_change=True)

    assert result.feedback is None and not result.applied
    assert result.warning is not None and NOT_RECORDED_WARNING in result.warning
    assert NO_CHANGE_WARNING not in result.warning
    assert list_feedback(topic.vault) == [] and len(fake.requests) == 1


def test_a_normal_turn_records_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text("La derivada es un límite.")

    result = _revise(topic, sync, fake, "¿Qué es la derivada?")

    assert result.feedback is None and result.warning is None
    assert list_feedback(topic.vault) == []


def test_the_session_id_goes_with_the_item(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, MEJORA)

    _revise(topic, sync, fake, "Apunta una mejora: PDF", session_id="2026-09-28-1")

    [item] = list_feedback(topic.vault)
    assert item.context.session_id == "2026-09-28-1"


# -- the study chat (Estudiar) --------------------------------------------------------------------


def test_the_study_chat_records_a_bug_without_touching_the_notes(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, BUG)
    before = read_notes(topic.vault, topic.subject, topic.topic)

    answer = _ask(topic, fake, "Esto es un bug: la foto no se guarda")

    assert answer.feedback is not None
    assert (answer.feedback.id, answer.feedback.kind) == ("fb-1", "bug")
    assert answer.reply == "He apuntado el bug: La foto no se guarda."
    assert answer.warning is None
    assert read_notes(topic.vault, topic.subject, topic.topic) == before

    [item] = list_feedback(topic.vault)
    assert (item.context.route, item.context.mode) == ("study", "estudiar")
    assert item.context.session_id is None
    assert item.context.excerpt == "Estudiante: Esto es un bug: la foto no se guarda"

    [request] = fake.requests
    assert [tool["name"] for tool in request.tools] == [FEEDBACK_TOOL]
    assert request.tool_choice == {"type": "auto"}
    assert "report_feedback" in _texts(request)

    [turn] = tutor_history(topic.vault, topic.subject, topic.topic).turns
    assert turn.feedback == answer.feedback


def test_the_study_chat_keeps_the_models_own_confirmation(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, MEJORA, text="He apuntado la mejora: PDF.")

    answer = _ask(topic, fake, "Apunta una mejora: exportar a PDF")

    assert answer.reply == "He apuntado la mejora: PDF."
    assert answer.feedback is not None and answer.feedback.kind == "mejora"


def test_a_malformed_call_in_the_study_chat_warns(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_tool(FEEDBACK_TOOL, "{not json", text="Vale.")

    answer = _ask(topic, fake, "Apunta una mejora: algo")

    assert answer.feedback is None
    assert answer.warning is not None and NOT_RECORDED_WARNING in answer.warning
    assert list_feedback(topic.vault) == []


def test_the_spoken_tutor_offers_no_tool(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("Es un límite.")

    _run(
        ask_tutor(topic.vault, topic.subject, topic.topic, "¿Qué es?", client=fake.client("editor"))
    )

    assert fake.requests[0].tools in (None, [])
