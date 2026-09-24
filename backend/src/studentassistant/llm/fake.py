"""`FakeClaude`: a scripted transport for tests. No network, no API key, no cost.

fake = FakeClaude()
fake.reply_text("Hola")
fake.reply_tool("record_topic", {"topic": "Derivadas"}, usage=Usage(input_tokens=900))
fake.fail(LLMRateLimitError("slow down"))          # the next request raises this
client = fake.client("observer")                   # or get_client("observer", transport=fake)
...
assert fake.requests[0].model == "claude-sonnet-5"
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from studentassistant.config import Settings
from studentassistant.llm.client import LLMClient, Sleep, get_client
from studentassistant.llm.errors import FakeClaudeExhaustedError, LLMError
from studentassistant.llm.types import LLMRequest, LLMResponse, Usage


async def no_sleep(_seconds: float) -> None:
    """A `sleep` that returns at once, so retry tests do not wait."""


class FakeClaude:
    """Replays scripted replies in order and records every request it receives."""

    def __init__(self, *, model: str | None = None) -> None:
        # The `model` reported back; by default the model of each request.
        self.model = model
        self.requests: list[LLMRequest] = []
        self._script: deque[LLMResponse | LLMError] = deque()
        self._tool_ids = 0

    # -- scripting -------------------------------------------------------------------------
    def reply(self, response: LLMResponse) -> FakeClaude:
        self._script.append(response)
        return self

    def reply_text(
        self, text: str, *, usage: Usage | None = None, stop_reason: str = "end_turn"
    ) -> FakeClaude:
        return self.reply(self._response([{"type": "text", "text": text}], stop_reason, usage))

    def reply_tool(
        self,
        name: str,
        input: dict[str, Any] | str,
        *,
        text: str | None = None,
        usage: Usage | None = None,
        stop_reason: str = "tool_use",
    ) -> FakeClaude:
        """A `tool_use` reply. A `str` input is kept verbatim (to script malformed JSON)."""
        self._tool_ids += 1
        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        content.append(
            {"type": "tool_use", "id": f"toolu_fake_{self._tool_ids}", "name": name, "input": input}
        )
        return self.reply(self._response(content, stop_reason, usage))

    def fail(self, error: LLMError) -> FakeClaude:
        """The next request raises `error` (e.g. `LLMRateLimitError` to exercise retries)."""
        self._script.append(error)
        return self

    # -- use -------------------------------------------------------------------------------
    def client(
        self, role: str, *, settings: Settings | None = None, sleep: Sleep = no_sleep
    ) -> LLMClient:
        """`get_client(role)` wired to this fake (and to a sleep that does not wait)."""
        return get_client(role, settings=settings, transport=self, sleep=sleep)

    @property
    def pending(self) -> int:
        """Scripted replies not consumed yet."""
        return len(self._script)

    async def send(self, request: LLMRequest) -> LLMResponse:
        # A deep copy, so later mutation of the caller's lists does not rewrite history.
        self.requests.append(request.model_copy(deep=True))
        await asyncio.sleep(0)
        if not self._script:
            raise FakeClaudeExhaustedError(
                f"FakeClaude got request #{len(self.requests)} but has no scripted reply left"
            )
        item = self._script.popleft()
        if isinstance(item, LLMError):
            raise item
        model = self.model or item.model or request.model
        return item.model_copy(update={"model": model}, deep=True)

    def _response(
        self, content: list[dict[str, Any]], stop_reason: str, usage: Usage | None
    ) -> LLMResponse:
        # `model` is filled in from the request when the reply is sent.
        return LLMResponse(
            model="", stop_reason=stop_reason, content=content, usage=usage or Usage()
        )
