"""`FakeClaude`: scripted replies in order, recorded requests, scripted failures."""

from __future__ import annotations

import asyncio

import pytest

from studentassistant.config import Settings
from studentassistant.llm import (
    FakeClaude,
    FakeClaudeExhaustedError,
    LLMAPIError,
    Usage,
    get_client,
)


def test_replays_text_tool_calls_and_usage_in_order(settings: Settings) -> None:
    fake = FakeClaude()
    fake.reply_text("Hola", usage=Usage(input_tokens=10, output_tokens=2))
    fake.reply_tool("record_topic", {"topic": "Derivadas"}, text="Vale.")
    client = fake.client("observer", settings=settings)

    async def run() -> None:
        first = await client.create([{"role": "user", "content": "Hola"}])
        assert first.text == "Hola"
        assert first.usage.input_tokens == 10
        assert first.stop_reason == "end_turn"
        assert first.model == "claude-sonnet-5"

        second = await client.create([{"role": "user", "content": "Tema"}])
        assert second.text == "Vale."
        assert second.stop_reason == "tool_use"
        (call,) = second.tool_calls
        assert call.name == "record_topic"
        assert call.parsed_input() == {"topic": "Derivadas"}

    asyncio.run(run())
    assert fake.pending == 0


def test_records_model_effort_system_messages_and_tools(settings: Settings) -> None:
    fake = FakeClaude().reply_text("ok")
    client = get_client("editor", settings=settings, transport=fake)
    tool = {"name": "t", "description": "d", "input_schema": {"type": "object"}}

    asyncio.run(
        client.create(
            [{"role": "user", "content": "Revisa"}],
            system="Eres el editor.",
            tools=[tool],
            prompt_hash="sha256:abc",
        )
    )

    (request,) = fake.requests
    assert request.model == "claude-opus-5-5"
    assert request.effort == "high"
    assert request.role == "editor"
    assert request.system[0]["text"] == "Eres el editor."
    assert request.messages == [{"role": "user", "content": "Revisa"}]
    assert request.tools[0]["name"] == "t"
    assert request.prompt_hash == "sha256:abc"


def test_requests_are_snapshots(settings: Settings) -> None:
    fake = FakeClaude().reply_text("ok")
    messages = [{"role": "user", "content": "uno"}]

    asyncio.run(fake.client("observer", settings=settings).create(messages))
    messages.append({"role": "assistant", "content": "dos"})

    assert len(fake.requests[0].messages) == 1


def test_a_scripted_failure_is_raised(settings: Settings) -> None:
    fake = FakeClaude().fail(LLMAPIError("bad request", status_code=400))

    with pytest.raises(LLMAPIError):
        asyncio.run(fake.client("observer", settings=settings).create([]))


def test_running_out_of_replies_fails_loudly(settings: Settings) -> None:
    fake = FakeClaude()

    with pytest.raises(FakeClaudeExhaustedError):
        asyncio.run(fake.client("observer", settings=settings).create([]))


def test_a_fixed_model_overrides_the_reported_one(settings: Settings) -> None:
    fake = FakeClaude(model="claude-fake").reply_text("ok")

    response = asyncio.run(fake.client("observer", settings=settings).create([]))

    assert response.model == "claude-fake"


def test_a_malformed_tool_input_is_kept_verbatim(settings: Settings) -> None:
    fake = FakeClaude().reply_tool("t", '{"a": ')
    response = asyncio.run(fake.client("observer", settings=settings).create([]))

    assert response.tool_calls[0].input_json == '{"a": '


def test_streams_the_text_of_a_reply_when_asked(settings: Settings) -> None:
    fake = FakeClaude().reply_tool("record_topic", {"topic": "Derivadas"}, text="Vale, apuntado.")
    fake.reply_text("Hola")
    client = fake.client("editor", settings=settings)
    deltas: list[str] = []

    async def on_text(text: str) -> None:
        deltas.append(text)

    async def run() -> None:
        response = await client.create([{"role": "user", "content": "Tema"}], on_text=on_text)
        assert response.text == "Vale, apuntado." and response.tool_calls
        # Without a sink nothing is streamed.
        await client.create([{"role": "user", "content": "Hola"}])

    asyncio.run(run())
    assert deltas == ["Vale, ", "apuntado."]
