"""Claude's server-side web tools: tool definitions, `pause_turn` resumption and result parsing.

The web search and web fetch tools run on Anthropic's servers inside one Messages API call: the
answer carries `server_tool_use` blocks (what Claude asked for), `web_search_tool_result` blocks
(the pages a search found) and `web_fetch_tool_result` blocks (a fetched page as a `document`).
A tool that fails does not raise: its result block holds an error object instead of the content.
A long server-side loop may stop with `stop_reason: "pause_turn"`; sending the conversation back
with the paused assistant turn appended resumes it (`run_server_tools`).

The tool versions come from `[llm] web_search_tool` / `web_fetch_tool` (the dynamic-filtering
`*_20260209` versions by default), so nothing here hard-codes one. Everything is plain dicts: no
`anthropic` type leaves `transport.py` (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from studentassistant.config import LlmSettings
from studentassistant.llm.client import LLMClient
from studentassistant.llm.transport import TextSink
from studentassistant.llm.types import LLMResponse

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_TOOL_NAME = "web_fetch"
PAUSE_TURN = "pause_turn"
DEFAULT_MAX_CONTINUATIONS = 5


def web_search_tool(
    settings: LlmSettings | None = None,
    *,
    max_uses: int | None = None,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> dict[str, Any]:
    """The web search tool definition (`[llm] web_search_tool` version)."""
    tool: dict[str, Any] = {
        "type": (settings or LlmSettings()).web_search_tool,
        "name": WEB_SEARCH_TOOL_NAME,
    }
    if max_uses is not None:
        tool["max_uses"] = max_uses
    if allowed_domains:
        tool["allowed_domains"] = list(allowed_domains)
    elif blocked_domains:
        tool["blocked_domains"] = list(blocked_domains)
    return tool


def web_fetch_tool(
    settings: LlmSettings | None = None,
    *,
    max_uses: int | None = None,
    max_content_tokens: int | None = None,
) -> dict[str, Any]:
    """The web fetch tool definition (`[llm] web_fetch_tool` version). Claude can only fetch a
    URL that already appears in the conversation."""
    tool: dict[str, Any] = {
        "type": (settings or LlmSettings()).web_fetch_tool,
        "name": WEB_FETCH_TOOL_NAME,
    }
    if max_uses is not None:
        tool["max_uses"] = max_uses
    if max_content_tokens is not None:
        tool["max_content_tokens"] = max_content_tokens
    return tool


@dataclass
class ServerToolRun:
    """Every response of one server-tool turn (more than one when it paused) and the messages
    the last request was sent with."""

    responses: list[LLMResponse]
    messages: list[dict[str, Any]]

    @property
    def final(self) -> LLMResponse:
        return self.responses[-1]

    @property
    def content(self) -> list[dict[str, Any]]:
        """The blocks of every response, in order."""
        return [block for response in self.responses for block in response.content]


async def run_server_tools(
    client: LLMClient,
    messages: list[dict[str, Any]],
    *,
    max_continuations: int = DEFAULT_MAX_CONTINUATIONS,
    on_text: TextSink | None = None,
    **create: Any,
) -> ServerToolRun:
    """`client.create(messages, **create)`, resumed while the answer stops with `pause_turn`.

    Each resumption sends the same messages plus the paused assistant turn (no extra user
    message: the API sees the trailing server tool use and continues), at most
    `max_continuations` times; the last response is returned as it is even if it paused again.
    Each call is its own ledger entry when the client is bound to one.
    """
    history = list(messages)
    responses: list[LLMResponse] = []
    while True:
        response = await client.create(history, on_text=on_text, **create)
        responses.append(response)
        if response.stop_reason != PAUSE_TURN or len(responses) > max_continuations:
            return ServerToolRun(responses=responses, messages=history)
        history = [*history, response.assistant_turn()]


# -- results ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WebSearchHit:
    """One page a web search found."""

    url: str
    title: str
    page_age: str | None = None


@dataclass(frozen=True)
class FetchedDocument:
    """One page web fetch retrieved. `text` is `None` for a binary document (a PDF: base64)."""

    url: str
    title: str | None
    retrieved_at: str | None
    media_type: str | None
    text: str | None
    data: str | None = None


@dataclass
class WebToolResults:
    """What the server tools of an answer returned: hits, fetched pages and error codes."""

    queries: list[str] = field(default_factory=list)
    hits: list[WebSearchHit] = field(default_factory=list)
    documents: list[FetchedDocument] = field(default_factory=list)
    search_errors: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)


def _error_code(content: Any) -> str | None:
    if isinstance(content, dict) and "error_code" in content:
        return str(content.get("error_code") or "unknown")
    return None


def parse_web_results(content: list[dict[str, Any]]) -> WebToolResults:
    """The hits, documents and errors of every web tool block in `content` (in order)."""
    results = WebToolResults()
    seen: set[str] = set()
    for block in content:
        kind = block.get("type")
        if kind == "server_tool_use" and block.get("name") == WEB_SEARCH_TOOL_NAME:
            query = (block.get("input") or {}).get("query")
            if isinstance(query, str):
                results.queries.append(query)
        elif kind == "web_search_tool_result":
            inner = block.get("content")
            error = _error_code(inner)
            if error is not None:
                results.search_errors.append(error)
                continue
            for hit in inner if isinstance(inner, list) else []:
                url = hit.get("url") if isinstance(hit, dict) else None
                if not isinstance(url, str) or url in seen:
                    continue
                seen.add(url)
                results.hits.append(
                    WebSearchHit(
                        url=url, title=str(hit.get("title") or url), page_age=hit.get("page_age")
                    )
                )
        elif kind == "web_fetch_tool_result":
            inner = block.get("content")
            error = _error_code(inner)
            if error is not None:
                results.fetch_errors.append(error)
                continue
            if not isinstance(inner, dict):
                continue
            document = inner.get("content") or {}
            source = document.get("source") or {}
            is_text = source.get("type") == "text"
            data = source.get("data")
            results.documents.append(
                FetchedDocument(
                    url=str(inner.get("url") or ""),
                    title=document.get("title"),
                    retrieved_at=inner.get("retrieved_at"),
                    media_type=source.get("media_type"),
                    text=data if is_text and isinstance(data, str) else None,
                    data=None if is_text else data,
                )
            )
    return results


__all__ = [
    "DEFAULT_MAX_CONTINUATIONS",
    "PAUSE_TURN",
    "WEB_FETCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_NAME",
    "FetchedDocument",
    "ServerToolRun",
    "WebSearchHit",
    "WebToolResults",
    "parse_web_results",
    "run_server_tools",
    "web_fetch_tool",
    "web_search_tool",
]
