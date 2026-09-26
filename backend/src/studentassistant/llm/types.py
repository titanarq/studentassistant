"""The request and response shapes the llm module exchanges with the rest of the backend.

They are plain Pydantic models so no other module ever sees an `anthropic` type (ADR-0004).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from studentassistant.config import Effort

Role = Literal["observer", "transcriber", "editor", "generator"]
ROLES: tuple[Role, ...] = ("observer", "transcriber", "editor", "generator")
Billing = Literal["api", "subscription"]


class Usage(BaseModel):
    """Token counts of one call, as the API reports them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Server-side tool uses (`usage.server_tool_use`): web searches are priced per use.
    web_search_requests: int = 0
    web_fetch_requests: int = 0


class ToolCall(BaseModel):
    """One `tool_use` block. `input_json` is the serialized input; parse it with `json`."""

    id: str
    name: str
    input_json: str

    def parsed_input(self) -> Any:
        """The input as Python data (`json.loads`); raises `json.JSONDecodeError` if malformed."""
        return json.loads(self.input_json)


class LLMRequest(BaseModel):
    """Exactly what is sent to the Messages API (plus the bookkeeping fields at the end)."""

    model: str
    max_tokens: int
    effort: Effort
    system: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: dict[str, Any] | None = None
    # Not sent: which role asked, and the hash of the prompt file used (for the ledger, #28).
    role: str
    prompt_hash: str | None = None

    def api_params(self) -> dict[str, Any]:
        """Keyword arguments for `messages.stream(...)`. `thinking` is never sent: omitting it runs
        adaptive thinking, and disabling it is rejected by Opus 5.5 (ADR-0004)."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self.messages,
            "output_config": {"effort": self.effort},
        }
        if self.system:
            params["system"] = self.system
        if self.tools:
            params["tools"] = self.tools
        if self.tool_choice is not None:
            params["tool_choice"] = self.tool_choice
        return params


class LLMResponse(BaseModel):
    """One final assistant message."""

    model: str
    stop_reason: str | None = None
    # Every content block as sent by the API (thinking included): append it unchanged as the
    # assistant turn when continuing a conversation.
    content: list[dict[str, Any]] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    # How the call is paid: `api` (per token, priced from `[llm.prices]`) or `subscription` (the
    # Claude Code backend on the user's plan). `reported_usd` is the cost the backend itself
    # reported for this call (Claude Code's `total_cost_usd`), recorded instead of the estimate.
    billing: Billing = "api"
    reported_usd: float | None = None

    @property
    def text(self) -> str:
        """All text blocks joined."""
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(
                id=b["id"],
                name=b["name"],
                input_json=b["input"] if isinstance(b["input"], str) else json.dumps(b["input"]),
            )
            for b in self.content
            if b.get("type") == "tool_use"
        ]

    def assistant_turn(self) -> dict[str, Any]:
        """This response as the `assistant` message to append to the conversation."""
        return {"role": "assistant", "content": self.content}
