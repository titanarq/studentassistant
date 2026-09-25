"""`conversations/<name>.jsonl`: an LLM role's conversation, appended one record at a time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from studentassistant.vault import (
    ConversationError,
    ConversationRecord,
    SecretRefused,
    TopicNotFoundError,
    Vault,
    append_conversation_record,
    conversation_path,
    create_subject,
    create_topic,
    read_conversation,
)

NAME = "observer-20260925-180000"
# Built from pieces so this source never holds a string that looks like a real key.
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "Xy7Kq2Lm9Np4Rs8Tv1Wz" * 2


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología")
    return subject.slug, create_topic(tmp_vault, subject.slug, "La célula").slug


def record(kind: str, **fields: object) -> ConversationRecord:
    return ConversationRecord(time=datetime(2026, 9, 25, 18, tzinfo=UTC), kind=kind, **fields)


def test_records_append_in_order_under_the_topic(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    assert read_conversation(tmp_vault, *topic, NAME) == []
    first = record("context", detail={"reason": "start"})
    second = record(
        "assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "Hola"}]},
        model="claude-sonnet-5",
        prompt_hash="sha256:abc",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    append_conversation_record(tmp_vault, *topic, NAME, first)
    append_conversation_record(tmp_vault, *topic, NAME, second)
    path = conversation_path(tmp_vault, *topic, NAME)
    assert path == tmp_vault.path / "subjects/biologia/topics/la-celula/conversations" / (
        NAME + ".jsonl"
    )
    assert read_conversation(tmp_vault, *topic, NAME) == [first, second]


@pytest.mark.parametrize("name", ["", "../ledger", "Observer", "a/b", "x" * 101])
def test_a_name_that_is_not_a_plain_file_name_is_refused(
    tmp_vault: Vault, topic: tuple[str, str], name: str
) -> None:
    with pytest.raises(ConversationError):
        append_conversation_record(tmp_vault, *topic, name, record("user"))


def test_an_unknown_topic_is_refused(tmp_vault: Vault) -> None:
    create_subject(tmp_vault, "Biología")
    with pytest.raises(TopicNotFoundError):
        append_conversation_record(tmp_vault, "biologia", "nada", NAME, record("user"))


def test_a_secret_looking_record_is_not_written(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    secret = record("user", message={"role": "user", "content": ANTHROPIC_KEY})
    with pytest.raises(SecretRefused):
        append_conversation_record(tmp_vault, *topic, NAME, secret)
    assert read_conversation(tmp_vault, *topic, NAME) == []


def test_record_times_are_aware_and_stored_in_utc() -> None:
    madrid = timezone(timedelta(hours=2))
    assert record("user").time.tzinfo == UTC
    local = ConversationRecord(time=datetime(2026, 9, 25, 20, tzinfo=madrid), kind="user")
    assert local.time == datetime(2026, 9, 25, 18, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ConversationRecord(time=datetime(2026, 9, 25, 18), kind="user")
