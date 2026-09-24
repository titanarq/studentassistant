"""Structured outputs: a strict tool, `tool_choice: auto` and an instruction to call it (ADR-0004).

Forced tool choice (`any`/`tool`) is rejected by Opus 5.5, so Claude is asked to call the tool;
its input is parsed with `json` and validated against the caller's Pydantic model. A missing or
invalid call is re-asked exactly once with the error; a second failure raises.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from studentassistant.llm.client import LLMClient
from studentassistant.llm.errors import RefusalError, StructuredOutputError
from studentassistant.llm.prompts import load_prompt
from studentassistant.llm.types import LLMResponse

INSTRUCTION_PROMPT = "structured-output"


class StructuredResult[T: BaseModel](BaseModel):
    """The validated value plus every response it took (one, or two after a re-ask)."""

    value: T
    responses: list[LLMResponse]


def strict_tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    """A `strict: true` tool whose input schema is `model`'s, in the form the API accepts."""
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": anthropic.transform_schema(model),
    }


def _check(response: LLMResponse, tool_name: str, model: type[BaseModel]) -> tuple[Any, str]:
    """`(value, "")` for a valid call, `(None, reason)` otherwise."""
    if response.stop_reason == "refusal":
        raise RefusalError(f"Claude declined the {tool_name!r} request")
    calls = [call for call in response.tool_calls if call.name == tool_name]
    if not calls:
        return None, f"you did not call the `{tool_name}` tool"
    call = calls[0]
    if response.stop_reason == "max_tokens":
        return None, "the tool input was cut off by max_tokens; give a shorter complete answer"
    try:
        data = json.loads(call.input_json)
    except json.JSONDecodeError as error:
        return None, f"the tool input is not valid JSON: {error}"
    try:
        return model.model_validate(data), ""
    except ValidationError as error:
        return None, f"the tool input does not match the schema: {error}"


def _reask_turn(response: LLMResponse, tool_name: str, reason: str) -> dict[str, Any]:
    """The user turn answering a failed attempt: a tool_result error for each call, plus text."""
    content: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": reason}
        for call in response.tool_calls
    ]
    content.append(
        {"type": "text", "text": f"Error: {reason}. Call the `{tool_name}` tool again, correctly."}
    )
    return {"role": "user", "content": content}


async def structured[T: BaseModel](
    client: LLMClient,
    messages: list[dict[str, Any]],
    output: type[T],
    *,
    tool_name: str,
    tool_description: str,
    system: str | list[str] | list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    prompt_hash: str | None = None,
    confirm_over_cap: bool = False,
) -> StructuredResult[T]:
    """Ask `client` for an `output` instance through the strict tool `tool_name`.

    Each underlying call (the re-ask included) is capped and recorded like `LLMClient.create`.
    Raises `StructuredOutputError` when the re-ask fails too, `RefusalError` on a refusal.
    """
    instruction = load_prompt(INSTRUCTION_PROMPT).render(tool_name=tool_name)
    system_parts: list[Any] = (
        [system] if isinstance(system, str) else list(system) if system else []
    )
    system_parts.append(instruction)
    tool = strict_tool(tool_name, tool_description, output)
    conversation = list(messages)
    responses: list[LLMResponse] = []
    reason = ""
    for _ in range(2):  # the first attempt, then exactly one re-ask
        response = await client.create(
            conversation,
            system=system_parts,
            tools=[tool],
            tool_choice={"type": "auto"},
            max_tokens=max_tokens,
            prompt_hash=prompt_hash,
            confirm_over_cap=confirm_over_cap,
        )
        responses.append(response)
        value, reason = _check(response, tool_name, output)
        if value is not None:
            return StructuredResult[output](value=value, responses=responses)
        conversation = [
            *conversation,
            response.assistant_turn(),
            _reask_turn(response, tool_name, reason),
        ]
    raise StructuredOutputError(tool_name, reason)
