"""Revision turns that answer a spoken request (`ChatRequestRef`), and older records as typed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor.revise import (
    ChatRequestRef,
    RevisionResult,
    chat_history,
    revise_notes,
)
from studentassistant.llm import FakeClaude
from studentassistant.vault import (
    ConversationRecord,
    GitSync,
    Vault,
    append_conversation_record,
    read_conversation,
)

REF = ChatRequestRef(
    request_id="req-2",
    summary="Pide un ejemplo de derivada",
    session_id="20260926-101500",
    segment_ids=["seg-4", "seg-5"],
    t_start_ms=154_000,
    t_end_ms=190_000,
    text="ponme un ejemplo de derivada",
)


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


def _turn(topic: ReviseTopic, fake: FakeClaude, message: str, **extra: object) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            message,
            client=fake.client("editor"),
            sync=GitSync(topic.vault),
            **extra,  # type: ignore[arg-type]
        )
    )


def test_a_spoken_turn_is_stored_with_its_request_and_read_back(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("Te lo explico: la derivada de x² es 2x.")

    result = _turn(topic, fake, REF.text, request=REF, turn_id="turn-abc")

    assert result.origin == "voice" and result.request == REF and result.turn_id == "turn-abc"
    [record] = [
        r
        for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")
        if r.kind == "revision"
    ]
    assert record.detail is not None
    assert record.detail["origin"] == "voice" and record.detail["turn_id"] == "turn-abc"
    assert record.detail["request"]["segment_ids"] == ["seg-4", "seg-5"]
    [turn] = chat_history(topic.vault, topic.subject, topic.topic).turns
    assert turn.origin == "voice" and turn.turn_id == "turn-abc"
    assert turn.request_summary == "Pide un ejemplo de derivada"
    assert turn.transcript == REF
    assert turn.message == "ponme un ejemplo de derivada"
    # The editor is told the message was spoken.
    text = fake.requests[0].messages[0]["content"][-1]["text"]
    assert "en voz alta" in text and text.rstrip().endswith("ponme un ejemplo de derivada")

    # And the next turn's history marks it as spoken.
    fake.reply_text("De nada.")
    _turn(topic, fake, "gracias")
    history = fake.requests[1].messages[0]["content"][-1]["text"]
    assert "Estudiante (en voz alta, transcrito): ponme un ejemplo de derivada" in history


def test_a_typed_turn_has_no_request(topic: ReviseTopic) -> None:
    fake = FakeClaude().reply_text("Vale.")
    result = _turn(topic, fake, "hola")
    assert result.origin == "typed" and result.request is None and result.turn_id is None
    text = fake.requests[0].messages[0]["content"][-1]["text"]
    assert "en voz alta" not in text
    [turn] = chat_history(topic.vault, topic.subject, topic.topic).turns
    assert turn.origin == "typed" and turn.request_summary is None and turn.transcript is None


def test_an_older_revision_record_reads_as_typed(topic: ReviseTopic) -> None:
    legacy = {
        "subject": topic.subject,
        "topic": topic.topic,
        "message": "Pon un ejemplo",
        "reply": "Hecho.",
        "applied": False,
    }
    append_conversation_record(
        topic.vault,
        topic.subject,
        topic.topic,
        "editor",
        ConversationRecord(time=datetime.now(UTC), kind="revision", detail=legacy),
    )
    [turn] = chat_history(topic.vault, topic.subject, topic.topic).turns
    assert turn.message == "Pon un ejemplo"
    assert turn.origin == "typed" and turn.turn_id is None
    assert turn.request_summary is None and turn.transcript is None
