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
import re
from collections import deque
from typing import Any

from studentassistant.config import Settings
from studentassistant.llm.client import LLMClient, Sleep, get_client
from studentassistant.llm.cost import Clock, LedgerBinding, utc_now
from studentassistant.llm.errors import FakeClaudeExhaustedError, LLMError
from studentassistant.llm.transport import TextSink
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
        self,
        role: str,
        *,
        settings: Settings | None = None,
        sleep: Sleep = no_sleep,
        ledger: LedgerBinding | None = None,
        clock: Clock = utc_now,
    ) -> LLMClient:
        """`get_client(role)` wired to this fake (and to a sleep that does not wait)."""
        return get_client(
            role, settings=settings, transport=self, sleep=sleep, ledger=ledger, clock=clock
        )

    @property
    def pending(self) -> int:
        """Scripted replies not consumed yet."""
        return len(self._script)

    async def send(self, request: LLMRequest, on_text: TextSink | None = None) -> LLMResponse:
        """The next scripted reply; with `on_text`, its text blocks are streamed word by word."""
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
        if on_text is not None:
            for block in item.content:
                if block.get("type") == "text":
                    for chunk in re.findall(r"\S+\s*|\s+", block.get("text") or ""):
                        await on_text(chunk)
        return item.model_copy(update={"model": model}, deep=True)

    def _response(
        self, content: list[dict[str, Any]], stop_reason: str, usage: Usage | None
    ) -> LLMResponse:
        # `model` is filled in from the request when the reply is sent.
        return LLMResponse(
            model="", stop_reason=stop_reason, content=content, usage=usage or Usage()
        )


# -- server-side web tool blocks, for scripting `FakeClaude.reply(LLMResponse(content=...))` -------


def web_search_blocks(
    query: str,
    hits: list[tuple[str, str]],
    *,
    tool_id: str = "srvtoolu_fake_search",
    error_code: str | None = None,
) -> list[dict[str, Any]]:
    """A `server_tool_use` web search and its `web_search_tool_result` (`hits`: `(url, title)`),
    or its error object with `error_code`."""
    result: Any
    if error_code is not None:
        result = {"type": "web_search_tool_result_error", "error_code": error_code}
    else:
        result = [
            {
                "type": "web_search_result",
                "url": url,
                "title": title,
                "encrypted_content": "fake",
                "page_age": None,
            }
            for url, title in hits
        ]
    return [
        {"type": "server_tool_use", "id": tool_id, "name": "web_search", "input": {"query": query}},
        {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": result},
    ]


def web_fetch_blocks(
    url: str,
    text: str | None = None,
    *,
    title: str | None = None,
    retrieved_at: str = "2026-09-25T10:00:00Z",
    media_type: str = "text/plain",
    data: str | None = None,
    tool_id: str = "srvtoolu_fake_fetch",
    error_code: str | None = None,
) -> list[dict[str, Any]]:
    """A `server_tool_use` web fetch and its `web_fetch_tool_result`: a text document (`text`),
    a base64 one (`data`, e.g. a PDF), or the error object with `error_code`."""
    result: dict[str, Any]
    if error_code is not None:
        result = {"type": "web_fetch_tool_error", "error_code": error_code}
    else:
        source = (
            {"type": "text", "media_type": media_type, "data": text or ""}
            if data is None
            else {"type": "base64", "media_type": media_type, "data": data}
        )
        document: dict[str, Any] = {"type": "document", "source": source}
        if title is not None:
            document["title"] = title
        result = {
            "type": "web_fetch_result",
            "url": url,
            "retrieved_at": retrieved_at,
            "content": document,
        }
    return [
        {"type": "server_tool_use", "id": tool_id, "name": "web_fetch", "input": {"url": url}},
        {"type": "web_fetch_tool_result", "tool_use_id": tool_id, "content": result},
    ]
