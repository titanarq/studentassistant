"""A client bound to a `LedgerBinding` appends one ledger entry per successful Claude call."""

from __future__ import annotations

import asyncio
import logging

import pytest
from ledger_helpers import FLAT, MODEL, NOON, SESSION, binding, capped_settings, make_topic
from pydantic import BaseModel

from studentassistant.config import Settings
from studentassistant.llm import (
    FakeClaude,
    LLMRateLimitError,
    LLMRetriesExhaustedError,
    Usage,
    structured,
)
from studentassistant.vault import Vault, ledger_path, read_all_ledgers, read_ledger

USAGE = Usage(
    input_tokens=1_000,
    output_tokens=200,
    cache_creation_input_tokens=3_000,
    cache_read_input_tokens=5_000,
)


class Answer(BaseModel):
    topic: str


def test_a_bound_call_records_every_field(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = FakeClaude().reply_text("Hola", usage=USAGE)
    client = fake.client(
        "observer",
        settings=capped_settings(settings),
        ledger=binding(tmp_vault, topic),
        clock=lambda: NOON,
    )

    asyncio.run(client.create([{"role": "user", "content": "hola"}], prompt_hash="sha256:abc"))

    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.time == NOON
    assert entry.role == "observer"
    assert entry.model == MODEL
    assert entry.prompt_hash == "sha256:abc"
    assert (entry.input_tokens, entry.output_tokens) == (1_000, 200)
    assert (entry.cache_write_tokens, entry.cache_read_tokens) == (3_000, 5_000)
    assert entry.estimated_usd == pytest.approx(9_200 * FLAT.input_per_mtok / 1_000_000)
    assert (entry.subject, entry.topic, entry.session) == (*topic, SESSION)


def test_default_prices_are_used(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = FakeClaude().reply_text("Hola", usage=Usage(input_tokens=1_000_000))

    asyncio.run(
        fake.client("editor", settings=settings, ledger=binding(tmp_vault, topic)).create([])
    )

    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.model == settings.llm.roles.editor.model
    assert entry.estimated_usd == pytest.approx(settings.llm.prices[entry.model].input_per_mtok)


def test_an_unpriced_model_is_recorded_with_no_cost(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = FakeClaude(model="claude-desconocido").reply_text("Hola", usage=USAGE)

    asyncio.run(
        fake.client("editor", settings=settings, ledger=binding(tmp_vault, topic)).create([])
    )

    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.model == "claude-desconocido"
    assert entry.estimated_usd is None
    assert entry.input_tokens == 1_000


def test_structured_reask_records_two_entries(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = (
        FakeClaude()
        .reply_text("se me olvidó la herramienta", usage=Usage(input_tokens=10))
        .reply_tool("record_topic", {"topic": "Derivadas"}, usage=Usage(input_tokens=20))
    )
    client = fake.client("observer", settings=settings, ledger=binding(tmp_vault, topic))

    result = asyncio.run(
        structured(
            client,
            [{"role": "user", "content": "¿de qué va?"}],
            Answer,
            tool_name="record_topic",
            tool_description="Record the topic.",
        )
    )

    assert result.value.topic == "Derivadas"
    assert [e.input_tokens for e in read_ledger(tmp_vault, *topic)] == [10, 20]


def test_without_a_binding_nothing_is_recorded(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = FakeClaude().reply_text("Hola", usage=USAGE)

    asyncio.run(fake.client("editor", settings=settings).create([]))

    assert not ledger_path(tmp_vault, *topic).exists()
    assert list(read_all_ledgers(tmp_vault)) == []


def test_a_failed_call_is_not_recorded(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    fake = FakeClaude().fail(LLMRateLimitError("despacio"))
    client = fake.client("editor", settings=settings, ledger=binding(tmp_vault, topic))
    client.max_attempts = 1

    with pytest.raises(LLMRetriesExhaustedError):
        asyncio.run(client.create([]))

    assert read_ledger(tmp_vault, *topic) == []


def test_a_ledger_write_failure_keeps_the_answer(
    tmp_vault: Vault, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    topic = make_topic(tmp_vault)
    gone = binding(tmp_vault, (topic[0], "tema-borrado"))
    fake = FakeClaude().reply_text("Hola", usage=USAGE)

    with caplog.at_level(logging.ERROR, logger="studentassistant.llm.client"):
        response = asyncio.run(fake.client("editor", settings=settings, ledger=gone).create([]))

    assert response.text == "Hola"
    assert any("could not record" in r.getMessage() for r in caplog.records)
