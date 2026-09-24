"""Structured outputs: strict tool + `tool_choice: auto` + instruction, validation, one re-ask."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from studentassistant.config import Settings
from studentassistant.llm import (
    FakeClaude,
    RefusalError,
    StructuredOutputError,
    load_prompt,
    strict_tool,
    structured,
)

USER = [{"role": "user", "content": "¿De qué trata esta página?"}]


class Topic(BaseModel):
    title: str
    confidence: float = Field(ge=0, le=1)
    keywords: list[str]


def ask(fake: FakeClaude, settings: Settings, **kwargs: object) -> object:
    client = fake.client("observer", settings=settings)
    return asyncio.run(
        structured(
            client,
            USER,
            Topic,
            tool_name="record_topic",
            tool_description="Record the topic of the page.",
            **kwargs,  # type: ignore[arg-type]
        )
    )


VALID = {"title": "Derivadas", "confidence": 0.9, "keywords": ["límite", "pendiente"]}


def test_a_valid_call_is_parsed_and_validated(settings: Settings) -> None:
    fake = FakeClaude().reply_tool("record_topic", VALID)

    result = ask(fake, settings, system="Eres el observador.")

    assert result.value == Topic(**VALID)  # type: ignore[attr-defined]
    (request,) = fake.requests
    assert request.tool_choice == {"type": "auto"}
    (tool,) = request.tools
    assert tool["name"] == "record_topic"
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert set(tool["input_schema"]["required"]) == {"title", "confidence", "keywords"}
    # The caller's system comes first, then the instruction to call the tool (cached prefix).
    assert request.system[0]["text"] == "Eres el observador."
    instruction = load_prompt("structured-output").render(tool_name="record_topic")
    assert request.system[-1]["text"] == instruction
    assert request.system[-1]["cache_control"] == {"type": "ephemeral"}


def test_an_invalid_input_is_re_asked_once_with_the_error(settings: Settings) -> None:
    fake = (
        FakeClaude()
        .reply_tool("record_topic", {"title": "Derivadas", "confidence": 3, "keywords": []})
        .reply_tool("record_topic", VALID)
    )

    result = ask(fake, settings)

    assert result.value.title == "Derivadas"  # type: ignore[attr-defined]
    assert len(result.responses) == 2  # type: ignore[attr-defined]
    first, second = fake.requests
    assert second.tool_choice == {"type": "auto"}
    assert second.messages[: len(USER)] == USER
    assistant, reask = second.messages[len(USER) :]
    assert assistant["role"] == "assistant"
    assert assistant["content"][-1]["type"] == "tool_use"
    assert reask["role"] == "user"
    tool_result, text = reask["content"]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == assistant["content"][-1]["id"]
    assert tool_result["is_error"] is True
    assert "confidence" in tool_result["content"]
    assert "record_topic" in text["text"]


def test_malformed_json_is_re_asked(settings: Settings) -> None:
    fake = (
        FakeClaude().reply_tool("record_topic", '{"title": "Deri').reply_tool("record_topic", VALID)
    )

    result = ask(fake, settings)

    assert result.value.keywords == ["límite", "pendiente"]  # type: ignore[attr-defined]
    assert "not valid JSON" in fake.requests[1].messages[-1]["content"][0]["content"]


def test_no_tool_call_is_re_asked(settings: Settings) -> None:
    fake = FakeClaude().reply_text("Trata de derivadas.").reply_tool("record_topic", VALID)

    result = ask(fake, settings)

    assert result.value.title == "Derivadas"  # type: ignore[attr-defined]
    reask = fake.requests[1].messages[-1]
    assert [block["type"] for block in reask["content"]] == ["text"]
    assert "did not call" in reask["content"][0]["text"]


def test_a_second_failure_raises_a_typed_error(settings: Settings) -> None:
    fake = (
        FakeClaude()
        .reply_tool("record_topic", {"title": "x"})
        .reply_tool("record_topic", {"title": "x"})
        .reply_tool("record_topic", VALID)  # never reached: only one re-ask
    )

    with pytest.raises(StructuredOutputError) as info:
        ask(fake, settings)

    assert info.value.tool_name == "record_topic"
    assert len(fake.requests) == 2
    assert fake.pending == 1


def test_a_refusal_raises(settings: Settings) -> None:
    fake = FakeClaude().reply_text("", stop_reason="refusal")

    with pytest.raises(RefusalError):
        ask(fake, settings)


def test_strict_tool_schema_is_api_ready() -> None:
    tool = strict_tool("record_topic", "Record it.", Topic)

    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"title", "confidence", "keywords"}
