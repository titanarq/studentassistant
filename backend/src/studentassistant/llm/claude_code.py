"""The Claude Code backend: Claude through the local `claude` CLI, headless, on the user's plan.

`ClaudeCodeTransport` implements `Transport`, so `LLMClient`, `structured`, the cost ledger and
the caps work unchanged on top of it (ADR-0004: every Claude call still goes through this module).

Token economy -- one long-lived process per conversation, never a polling loop:

- A conversation is a `claude -p --input-format stream-json --output-format stream-json` process
  started with the request's model, effort and system prompt. Each request is one user turn
  written as a JSON line on its stdin; its answer is read line by line from stdout up to the
  turn's `result` event. The CLI keeps the conversation (and its prompt cache) itself.
- The backend's callers resend the whole message list on every call (the Messages API is
  stateless). A request whose messages are exactly what a live process has already seen (the
  earlier messages plus the assistant turn this transport returned, `cache_control` markers
  ignored) followed by new user turns is sent to that process as only those new turns. Anything
  else starts a new process; an earlier history is then rendered into its first turn.
- A process with no turn for `idle_timeout_seconds` is closed by a timer (`call_later`); at most
  `max_processes` live at once (the least recently used idle one is closed first).

Claude is only a text/JSON responder here: every built-in tool is disabled (`--tools ""`), no MCP
server, no slash command, no user or project settings, no session persistence, and the process
runs in a private, empty working directory. Client tools (the strict tool of `structured`, the
observer's and the editor's tools) are described in the system prompt and answered as one JSON
object, which this module turns back into `tool_use` blocks; `tool_result` blocks go back as text.

Web search / web fetch (#306): a request of the `web_role` (`[sources] web_search_role`) carrying
the API's web search / web fetch server tools runs on a process started with the CLI's own
built-in `WebSearch` / `WebFetch` tools allowed (their names come from `[llm.claude_code]`), and
those only. The CLI's tool events are turned back into the API's blocks (`server_tool_use`,
`web_search_tool_result`, `web_fetch_tool_result`), so `llm.web.parse_web_results` and the
sources that use it do not branch on the backend. The CLI's WebFetch answers with the page as its
own small model rewrites it for a prompt, so the request asks for the page verbatim. A web tool
turned off by configuration, or missing from the CLI, is a `WebToolsUnavailableError`; a fetch
that returns nothing is a `web_fetch_tool_result` error with `WEB_TOOL_UNAVAILABLE_ERROR`. Server
tools in any other role's request are refused.

Usage and cost come from each turn's `result` event: its `usage` is the turn's, while
`total_cost_usd` is the process's running total, so the turn's cost is the difference.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from studentassistant.config import DEFAULT_STRUCTURED_REASKS_CLAUDE_CODE, ClaudeCodeSettings
from studentassistant.llm.errors import (
    LLMAPIError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    WebToolsUnavailableError,
)
from studentassistant.llm.json_repair import loads_tolerant
from studentassistant.llm.transport import TextSink
from studentassistant.llm.types import Billing, LLMRequest, LLMResponse, Usage
from studentassistant.llm.web import (
    WEB_FETCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    WEB_TOOL_UNAVAILABLE_ERROR,
)

logger = logging.getLogger(__name__)

# One stream-json line can carry a whole long answer (or an echoed image): no small line limit.
STREAM_LIMIT_BYTES = 64 * 1024 * 1024
# The CLI's cap on the answer's length, set per process from the request's `max_tokens`.
MAX_OUTPUT_TOKENS_ENV_VAR = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
STDERR_TAIL_LINES = 40
# How long a closing process may take to exit after its stdin is closed before it is killed.
CLOSE_GRACE_SECONDS = 5.0

TOOL_ID_PREFIX = "toolu_cc_"


def base_command(
    settings: ClaudeCodeSettings,
    *,
    model: str,
    effort: str,
    system_prompt_file: Path,
    tools: Sequence[str] = (),
) -> list[str]:
    """The `claude` command line of one conversation process: no built-in tool but `tools` (the
    CLI web tools of a web request), which are also pre-approved."""
    allowed = ",".join(tools)
    return [
        settings.executable,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        model,
        "--effort",
        effort,
        "--system-prompt-file",
        str(system_prompt_file),
        "--tools",
        allowed,
        *(["--allowedTools", allowed] if tools else []),
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--safe-mode",
        *settings.extra_args,
    ]


# -- messages -------------------------------------------------------------------------------------


def _strip_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    return value


def _blocks(content: Any) -> list[dict[str, Any]]:
    """A message's content as a list of blocks, without `cache_control` markers."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [_strip_cache_control(block) for block in content or []]


def normalized_message(message: dict[str, Any]) -> str:
    """A comparable form of a message: role and blocks, cache breakpoints ignored."""
    return json.dumps(
        {"role": message.get("role"), "content": _blocks(message.get("content"))},
        sort_keys=True,
        ensure_ascii=False,
    )


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_call_json(calls: Sequence[dict[str, Any]]) -> str:
    return json.dumps(
        {"tool_calls": [{"name": c.get("name"), "input": c.get("input")} for c in calls]},
        ensure_ascii=False,
    )


def render_blocks(content: Any, tool_names: dict[str, str]) -> list[dict[str, Any]]:
    """A message's blocks as the CLI takes them: text, images and documents as they are;
    `tool_result` and `tool_use` as text; thinking dropped."""
    rendered: list[dict[str, Any]] = []
    for block in _blocks(content):
        kind = block.get("type")
        if kind == "text":
            rendered.append(_text(block.get("text", "")))
        elif kind in ("image", "document"):
            rendered.append(block)
        elif kind == "tool_result":
            tool_id = str(block.get("tool_use_id", ""))
            name = tool_names.get(tool_id)
            label = f"`{name}` ({tool_id})" if name else f"({tool_id})"
            status = " ERROR" if block.get("is_error") else ""
            inner = block.get("content")
            extra: list[dict[str, Any]] = []
            if isinstance(inner, list):
                texts = []
                for item in inner:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    else:
                        extra.append(item)
                body = "\n".join(texts)
            else:
                body = "" if inner is None else str(inner)
            rendered.append(_text(f"[tool_result of {label}{status}]\n{body}"))
            rendered.extend(extra)
        elif kind == "tool_use":
            rendered.append(_text(_tool_call_json([block])))
        elif kind in ("thinking", "redacted_thinking"):
            continue
        elif kind in ("server_tool_use", "web_search_tool_result", "web_fetch_tool_result"):
            rendered.append(_text(_render_web_block(block)))
        else:
            rendered.append(_text(json.dumps(block, ensure_ascii=False)))
    return rendered


def _render_web_block(block: dict[str, Any]) -> str:
    """A web tool block of an earlier answer as a short text (a new process's history)."""
    kind = block.get("type")
    if kind == "server_tool_use":
        return f"[{block.get('name')} {json.dumps(block.get('input'), ensure_ascii=False)}]"
    inner = block.get("content")
    if isinstance(inner, dict) and "error_code" in inner:
        return f"[{kind}: error {inner.get('error_code')}]"
    if kind == "web_search_tool_result" and isinstance(inner, list):
        hits = [f"- {h.get('title')} <{h.get('url')}>" for h in inner if isinstance(h, dict)]
        return "[web search results]\n" + "\n".join(hits)
    if isinstance(inner, dict):
        source = (inner.get("content") or {}).get("source") or {}
        return f"[fetched page <{inner.get('url')}>]\n{source.get('data') or ''}"
    return f"[{kind}]"


def render_turns(messages: Sequence[dict[str, Any]], tool_names: dict[str, str]) -> list[dict]:
    """The content of one CLI user turn carrying `messages` (all user turns, in order)."""
    content: list[dict[str, Any]] = []
    for message in messages:
        content.extend(render_blocks(message.get("content"), tool_names))
    return content or [_text("(empty)")]


def render_history(messages: Sequence[dict[str, Any]], tool_names: dict[str, str]) -> list[dict]:
    """The first turn of a new process given a whole history: a transcript, then the last turn."""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return render_turns(messages, tool_names)
    content: list[dict[str, Any]] = [
        _text(
            "The conversation so far follows, turn by turn; the assistant turns are your own "
            "earlier replies. Continue it: answer the last user turn."
        )
    ]
    for message in messages:
        role = message.get("role", "user")
        header = "assistant (your earlier reply)" if role == "assistant" else "user"
        content.append(_text(f"=== {header} ==="))
        content.extend(render_blocks(message.get("content"), tool_names))
    return content


# -- client tools -------------------------------------------------------------------------------

_TOOLS_INSTRUCTION = """\
# Tools

You cannot run anything yourself. The tools below are called through a text protocol: to call \
one or more of them, your whole reply must be exactly one JSON object and nothing else, of the \
form

{"tool_calls": [{"name": "<tool name>", "input": {<the tool's input, matching its schema>}}]}

To answer without calling a tool, reply in plain text as usual. The result of a call comes back \
in the next user turn as a block starting with `[tool_result of ...]`.
"""


# What a WebFetch call asks the CLI's small model to return: the page itself, not an answer.
WEB_FETCH_PROMPT = (
    "Return the whole main content of the page verbatim, as Markdown: every heading, paragraph, "
    "list, table and formula in order. Do not summarize, shorten, translate or comment on it."
)


def is_server_tool(tool: dict[str, Any]) -> bool:
    return tool.get("type") not in (None, "custom")


def web_tool_kind(tool: dict[str, Any]) -> str | None:
    """`web_search` / `web_fetch` for the API's web server tools, else `None`."""
    if not is_server_tool(tool):
        return None
    kind = str(tool.get("type") or "")
    for name in (WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME):
        if kind.startswith(name) and tool.get("name", name) == name:
            return name
    return None


def _refused(kind: Any) -> LLMAPIError:
    return LLMAPIError(
        f"the server tool {kind!r} needs the Anthropic API: it is not available with "
        "the claude-code backend ([llm] backend)"
    )


def _web_section(tools: Sequence[dict[str, Any]], cli_tools: dict[str, str]) -> str:
    lines = [
        "# Web tools",
        "",
        "You can run these built-in tools yourself (directly, not through a JSON reply):",
    ]
    for tool in tools:
        kind = web_tool_kind(tool)
        name = cli_tools.get(kind or "")
        if name is None:
            continue
        limit = tool.get("max_uses")
        uses = f" Use it at most {limit} time(s)." if isinstance(limit, int) else ""
        if kind == WEB_SEARCH_TOOL_NAME:
            domains = ""
            if tool.get("allowed_domains"):
                domains = " Only search these domains: " + ", ".join(tool["allowed_domains"]) + "."
            elif tool.get("blocked_domains"):
                domains = " Never use these domains: " + ", ".join(tool["blocked_domains"]) + "."
            lines.append(f"- `{name}` searches the web.{uses}{domains}")
        else:
            lines.append(
                f"- `{name}` fetches a web page. Call it with the page's URL and exactly this "
                f"prompt: {WEB_FETCH_PROMPT!r}{uses}"
            )
    return "\n".join(lines) + "\n"


def tools_instruction(
    tools: Sequence[dict[str, Any]],
    tool_choice: dict[str, Any] | None,
    cli_tools: dict[str, str] | None = None,
) -> str:
    """The system-prompt section describing `tools`; empty without tools.

    `cli_tools` maps the web server tools allowed on this process (`web_search`, `web_fetch`) to
    the CLI tool that runs them. Raises `LLMAPIError` for any other server tool.
    """
    if not tools:
        return ""
    cli_tools = cli_tools or {}
    parts: list[str] = []
    custom = [tool for tool in tools if not is_server_tool(tool)]
    for tool in tools:
        if is_server_tool(tool) and web_tool_kind(tool) not in cli_tools:
            raise _refused(tool.get("type"))
    if any(is_server_tool(tool) for tool in tools):
        parts.append(_web_section(tools, cli_tools))
    if custom:
        parts.append(_TOOLS_INSTRUCTION)
    for tool in custom:
        schema = json.dumps(tool.get("input_schema", {}), ensure_ascii=False, indent=1)
        parts.append(
            f"## {tool.get('name')}\n\n{tool.get('description', '')}\n\n"
            f"Input schema (JSON Schema):\n```json\n{schema}\n```\n"
        )
    if tool_choice is not None and tool_choice.get("type") == "none":
        parts.append("Do not call any tool in your next reply.\n")
    return "\n".join(parts)


def system_text(request: LLMRequest, cli_tools: dict[str, str] | None = None) -> str:
    """The whole system prompt of the request's conversation: its blocks, then its tools."""
    text = "\n\n".join(
        block.get("text", "") for block in request.system if block.get("type", "text") == "text"
    )
    tools = tools_instruction(request.tools, request.tool_choice, cli_tools)
    return "\n\n".join(part for part in (text, tools) if part)


_FENCE_OPENER = re.compile(r"```[a-zA-Z]*\s*$")
_TOOL_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    """`(prose before the call, calls)` when `text` is a tool-call reply, else `None`.

    The call is parsed with `loads_tolerant`, so fences, surrounding prose and small defects (a
    missing or trailing `,`, an unescaped inner quote) are repaired. A call whose JSON is still
    malformed keeps its raw input as a string, so the caller reports the exact JSON error (as
    `structured` does) instead of "you did not call the tool".
    """
    marker = text.find('"tool_calls"')
    if marker < 0:
        return None
    start = text.rfind("{", 0, marker)
    end = text.rfind("}")
    if start < 0:
        return None
    prose = _FENCE_OPENER.sub("", text[:start]).strip()  # a ```json fence around the call
    raw = text[start : end + 1] if end > start else text[start:]  # cut off: no closing brace
    try:
        data = loads_tolerant(raw)  # a small defect (a missing `,`, #320) is repaired
    except json.JSONDecodeError:
        names = _TOOL_NAME.findall(raw)
        if not names:
            return None
        return prose, [{"name": names[0], "input": raw}]
    if not isinstance(data, dict) or not isinstance(data.get("tool_calls"), list):
        return None
    calls = [
        {"name": call["name"], "input": _call_input(call.get("input", {}))}
        for call in data["tool_calls"]
        if isinstance(call, dict) and isinstance(call.get("name"), str)
    ]
    return (prose, calls) if calls else None


def _call_input(value: Any) -> Any:
    """A call's input; one written as a JSON string (`"input": "{...}"`) is parsed too."""
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            return loads_tolerant(value)
        except json.JSONDecodeError:
            return value
    return value


# -- web tools ----------------------------------------------------------------------------------

_LINKS = re.compile(r"Links:\s*(\[.*?\])\s*(?:\n|$)", re.DOTALL)


def _tool_result_text(block: dict[str, Any]) -> str:
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(str(item.get("text", "")) for item in inner if isinstance(item, dict))
    return ""


def _search_hits(result: Any, text: str) -> list[dict[str, Any]] | None:
    """The `{url, title}` pages of a CLI WebSearch result (its `tool_use_result`, else the
    `Links: [...]` line of its text); `None` when it has none to read."""
    found: list[dict[str, Any]] | None = None
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        found = []
        for item in result["results"]:
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                found.extend(hit for hit in item["content"] if isinstance(hit, dict))
    if not found:
        match = _LINKS.search(text)
        if match:
            with contextlib.suppress(json.JSONDecodeError):
                links = json.loads(match.group(1))
                if isinstance(links, list):
                    found = [hit for hit in links if isinstance(hit, dict)]
    if found is None:
        return None
    return [
        {"type": "web_search_result", "url": hit["url"], "title": str(hit.get("title") or "")}
        for hit in found
        if isinstance(hit.get("url"), str)
    ]


class _WebTurn:
    """The CLI WebSearch / WebFetch calls of one turn, as the API's web tool blocks."""

    def __init__(self, cli_tools: dict[str, str]) -> None:
        self.kinds = {name: kind for kind, name in cli_tools.items()}  # CLI name -> API kind
        self.calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self.blocks: list[dict[str, Any]] = []
        self.searches = 0
        self.fetches = 0
        self.fetched_text = False

    def on_assistant(self, block: dict[str, Any]) -> None:
        kind = self.kinds.get(str(block.get("name")))
        if block.get("type") != "tool_use" or kind is None:
            return
        tool_id = str(block.get("id") or f"srvtoolu_cc_{uuid.uuid4().hex[:24]}")
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        self.calls[tool_id] = (kind, tool_input)
        api_input = (
            {"query": tool_input.get("query")}
            if kind == WEB_SEARCH_TOOL_NAME
            else {"url": tool_input.get("url")}
        )
        self.blocks.append(
            {"type": "server_tool_use", "id": tool_id, "name": kind, "input": api_input}
        )
        if kind == WEB_SEARCH_TOOL_NAME:
            self.searches += 1
        else:
            self.fetches += 1

    def on_user(self, event: dict[str, Any]) -> None:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        # `tool_use_result` (the tool's structured output) is per event: only one result's.
        structured = event.get("tool_use_result") if len(results) == 1 else None
        for block in results:
            call = self.calls.get(str(block.get("tool_use_id")))
            if call is None:
                continue
            kind, tool_input = call
            tool_id = str(block.get("tool_use_id"))
            text = _tool_result_text(block)
            if kind == WEB_SEARCH_TOOL_NAME:
                self.blocks.append(self._search_result(tool_id, block, structured, text))
            else:
                self.blocks.append(
                    self._fetch_result(tool_id, block, structured, text, tool_input, event)
                )

    def _search_result(
        self, tool_id: str, block: dict[str, Any], structured: Any, text: str
    ) -> dict[str, Any]:
        hits = None if block.get("is_error") else _search_hits(structured, text)
        content: Any = (
            {"type": "web_search_tool_result_error", "error_code": "unavailable"}
            if hits is None
            else hits
        )
        return {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": content}

    def _fetch_result(
        self,
        tool_id: str,
        block: dict[str, Any],
        structured: Any,
        text: str,
        tool_input: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        info = structured if isinstance(structured, dict) else {}
        page = info.get("result") if isinstance(info.get("result"), str) else text
        if block.get("is_error"):
            content: dict[str, Any] = {"type": "web_fetch_tool_error", "error_code": "fetch_failed"}
        elif not page.strip():
            content = {"type": "web_fetch_tool_error", "error_code": WEB_TOOL_UNAVAILABLE_ERROR}
        else:
            self.fetched_text = True
            content = {
                "type": "web_fetch_result",
                "url": str(info.get("url") or tool_input.get("url") or ""),
                "retrieved_at": event.get("timestamp"),
                "content": {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": page},
                    "title": None,
                },
            }
        return {"type": "web_fetch_tool_result", "tool_use_id": tool_id, "content": content}

    def finish(self, wanted: set[str]) -> None:
        """A web fetch was asked for but none returned text: say the CLI could not serve it."""
        if (
            WEB_FETCH_TOOL_NAME in wanted
            and not self.fetched_text
            and not any(block.get("type") == "web_fetch_tool_result" for block in self.blocks)
        ):
            self.blocks.append(
                {
                    "type": "web_fetch_tool_result",
                    "tool_use_id": f"srvtoolu_cc_{uuid.uuid4().hex[:24]}",
                    "content": {
                        "type": "web_fetch_tool_error",
                        "error_code": WEB_TOOL_UNAVAILABLE_ERROR,
                    },
                }
            )


@dataclass
class _TurnResult:
    event: dict[str, Any]
    text: str
    model: str | None
    web: _WebTurn


# -- the transport ------------------------------------------------------------------------------

Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


class _Conversation:
    """One live `claude` process and what it has seen."""

    def __init__(
        self, key: str, process: asyncio.subprocess.Process, system_file: Path, model: str
    ) -> None:
        self.key = key
        self.process = process
        self.system_file = system_file
        self.model = model
        self.loop = asyncio.get_running_loop()
        self.seen: list[str] = []
        self.tool_names: dict[str, str] = {}
        self.total_cost_usd = 0.0
        self.billing: Billing = "subscription"
        self.busy = False
        self.last_used = time.monotonic()
        self.idle_handle: asyncio.TimerHandle | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self.stderr_task = asyncio.ensure_future(self._drain_stderr())

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while line := await stream.readline():
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    def stderr_summary(self) -> str:
        return " | ".join(line for line in self.stderr_tail if line)[-800:]

    async def _write(self, content: list[dict[str, Any]]) -> None:
        line = json.dumps(
            {"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False
        )
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing():
            raise await self._exit_error("before reading its input")
        try:
            stdin.write(line.encode("utf-8") + b"\n")
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise await self._exit_error("before reading its input") from error

    async def turn(
        self,
        content: list[dict[str, Any]],
        on_text: TextSink | None,
        expect_tools: bool,
        cli_tools: dict[str, str] | None = None,
    ) -> _TurnResult:
        """Send one user turn; return its `result` event, the answer's text, its model and the
        web tool calls it made."""
        await self._write(content)
        stdout = self.process.stdout
        assert stdout is not None
        sink = _TextStream(on_text, expect_tools)
        web = _WebTurn(cli_tools or {})
        texts: list[str] = []
        model: str | None = None
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as error:
                raise LLMConnectionError(f"unreadable claude output: {error}") from error
            if not raw:
                raise await self._exit_error("before answering")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("claude: non-JSON output line skipped: %r", raw[:200])
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                source = event.get("apiKeySource")
                self.billing = "subscription" if source in (None, "none") else "api"
                offered = event.get("tools")
                missing = [n for n in (cli_tools or {}).values() if n not in (offered or [])]
                if isinstance(offered, list) and missing:
                    raise WebToolsUnavailableError(
                        f"this claude CLI does not offer the {', '.join(missing)} tool(s) "
                        "a web request needs ([llm.claude_code] web_search_tool / web_fetch_tool)"
                    )
            elif kind == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta":
                    await sink.feed(delta.get("text", ""))
            elif kind == "assistant":
                message = event.get("message") or {}
                model = message.get("model") or model
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif isinstance(block, dict):
                        web.on_assistant(block)
            elif kind == "user":
                web.on_user(event)
            elif kind == "result":
                text = "".join(texts)
                if not text and isinstance(event.get("result"), str):
                    text = event["result"]
                if not event.get("is_error") and event.get("subtype", "success") == "success":
                    await sink.finish(text, is_tool_call=expect_tools and _is_call(text))
                web.finish(set(cli_tools or {}))
                return _TurnResult(event, text, model, web)

    async def _wait_exit(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), CLOSE_GRACE_SECONDS)

    async def _exit_error(self, when: str) -> LLMConnectionError:
        """The error for a process that went away: its exit code and the tail of its stderr
        (e.g. the CLI's own "System prompt file not found"), read to its end first."""
        await self._wait_exit()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self.stderr_task), CLOSE_GRACE_SECONDS)
        return LLMConnectionError(
            f"the claude process exited (code {self.process.returncode}) {when}: "
            f"{self.stderr_summary() or 'no error output'}"
        )

    def cancel_idle(self) -> None:
        if self.idle_handle is not None:
            self.idle_handle.cancel()
            self.idle_handle = None

    async def close(self) -> None:
        """Close stdin (the CLI exits at end of input), then kill it if it lingers."""
        self.cancel_idle()
        if self.alive:
            stdin = self.process.stdin
            if stdin is not None and not stdin.is_closing():
                with contextlib.suppress(Exception):
                    stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), CLOSE_GRACE_SECONDS)
            except TimeoutError:
                self.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.process.wait(), CLOSE_GRACE_SECONDS)
        # Read both pipes to their end so the subprocess transport closes with the process.
        with contextlib.suppress(Exception):
            if self.process.stdout is not None:
                await asyncio.wait_for(self.process.stdout.read(), CLOSE_GRACE_SECONDS)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.stderr_task, CLOSE_GRACE_SECONDS)
        self.stderr_task.cancel()
        self.system_file.unlink(missing_ok=True)

    def kill(self) -> None:
        if self.alive:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.kill()


def _is_call(text: str) -> bool:
    return parse_tool_calls(text) is not None


_CALL_START = re.compile(r'(?:^|\n)[ \t]*(?:\{|```)|\{\s*"tool_calls"')
"""Where a tool call may start: a line opening with `{` or a code fence, or `{"tool_calls"`."""
_CALL_OPENER = '"tool_calls"'


class _TextStream:
    """Passes text deltas on, holding back from where a tool call may start (`_CALL_START`).

    The editor writes its reply to the student first and its tool call after it, in the same
    text: the call's JSON carries the whole change (the notes, a Mermaid diagram), which must not
    reach the chat. So the prose streams, and from a possible call on the text is held: dropped
    when the answer turns out to be a tool call, sent at the end otherwise. A request with no
    tools streams everything.
    """

    def __init__(self, on_text: TextSink | None, expect_tools: bool) -> None:
        self.on_text = on_text
        self.expect_tools = expect_tools
        self.pending = ""
        self.fed = False
        self.holding = False

    async def feed(self, delta: str) -> None:
        if self.on_text is None or not delta:
            return
        self.fed = True
        self.pending += delta
        if self.holding:
            return
        if not self.expect_tools:
            await self._send(len(self.pending))
            return
        match = _CALL_START.search(self.pending)
        if match is not None:
            self.holding = True
            await self._send(match.start())
            return
        await self._send(self._safe_end())

    def _safe_end(self) -> int:
        """How much of `pending` cannot become the start of a call, whatever comes next."""
        line = self.pending.rfind("\n") + 1
        tail = self.pending[line:].lstrip(" \t")
        if tail == "" or tail in ("`", "``"):
            return line
        brace = self.pending.rfind("{")
        if brace >= 0:
            after = self.pending[brace + 1 :].lstrip()
            if _CALL_OPENER.startswith(after[: len(_CALL_OPENER)]):
                return brace
        return len(self.pending)

    async def _send(self, end: int) -> None:
        text, self.pending = self.pending[:end], self.pending[end:]
        if text and self.on_text is not None:
            await self.on_text(text)

    async def finish(self, text: str, *, is_tool_call: bool) -> None:
        if self.on_text is None or is_tool_call:
            return
        if not self.fed:  # nothing streamed: the whole answer at once
            if text:
                await self.on_text(text)
            return
        await self._send(len(self.pending))


def _error_for(event: dict[str, Any]) -> LLMError:
    """The llm-module error of a failed turn's `result` event."""
    status = event.get("api_error_status")
    errors = event.get("errors")
    message = (
        (event.get("result") if isinstance(event.get("result"), str) else None)
        or ("; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else None)
        or str(event.get("subtype") or "error")
    )
    message = f"claude: {message}"
    if isinstance(status, int):
        if status == 429:
            return LLMRateLimitError(message)
        if status >= 500:
            return LLMServerError(message, status_code=status)
        return LLMAPIError(message, status_code=status)
    return LLMAPIError(message)


def _usage(event: dict[str, Any], web: _WebTurn | None = None) -> Usage:
    """The turn's usage. Web searches are counted as the `result` event reports them
    (`usage.server_tool_use`, else the per-model `modelUsage.*.webSearchRequests`: the CLI's
    WebSearch runs on a helper model); the CLI reports no fetch count, so its WebFetch calls
    are counted (a count reported as 0 falls back to the calls seen)."""
    usage = event.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    searches = int(server.get("web_search_requests") or 0)
    if not searches and isinstance(event.get("modelUsage"), dict):
        searches = sum(
            int(model.get("webSearchRequests") or 0)
            for model in event["modelUsage"].values()
            if isinstance(model, dict)
        )
    fetches = int(server.get("web_fetch_requests") or 0)
    if web is not None:
        searches = searches or web.searches
        fetches = fetches or web.fetches
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        web_search_requests=searches,
        web_fetch_requests=fetches,
    )


_live_transports: weakref.WeakSet[ClaudeCodeTransport] = weakref.WeakSet()


@atexit.register
def _kill_all_at_exit() -> None:  # pragma: no cover - interpreter shutdown
    for transport in list(_live_transports):
        transport.kill_all()


class ClaudeCodeTransport:
    """`Transport` over long-lived headless `claude` processes, one per conversation.

    One instance should serve the whole backend process (the server passes one to every feature),
    so conversations are found again across requests. `web_role` is the role whose requests may
    carry the web search / web fetch server tools (`[sources] web_search_role`; none: every
    server tool is refused). `spawn` replaces `asyncio.create_subprocess_exec` and `monotonic`
    the clock (tests).
    """

    # A tool call written as text is malformed more often than an API one (#320).
    default_structured_reasks = DEFAULT_STRUCTURED_REASKS_CLAUDE_CODE

    def __init__(
        self,
        settings: ClaudeCodeSettings | None = None,
        *,
        web_role: str | None = None,
        spawn: Spawn | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or ClaudeCodeSettings()
        self.web_role = web_role
        self._spawn: Spawn = spawn or asyncio.create_subprocess_exec
        self._monotonic = monotonic
        self._conversations: list[_Conversation] = []
        self._closing: set[asyncio.Future[None]] = set()
        _live_transports.add(self)

    @property
    def process_count(self) -> int:
        """How many `claude` processes are live (for tests and diagnostics)."""
        return sum(1 for conversation in self._conversations if conversation.alive)

    def cli_web_tools(self, request: LLMRequest) -> dict[str, str]:
        """The CLI tools the request's web server tools run on (`{"web_search": "WebSearch"}`);
        empty without any. Raises `LLMAPIError` for a server tool this request may not use and
        `WebToolsUnavailableError` for a web tool turned off in `[llm.claude_code]`."""
        server = [tool for tool in request.tools if is_server_tool(tool)]
        if not server:
            return {}
        names = {
            WEB_SEARCH_TOOL_NAME: self.settings.web_search_tool.strip(),
            WEB_FETCH_TOOL_NAME: self.settings.web_fetch_tool.strip(),
        }
        tools: dict[str, str] = {}
        for tool in server:
            kind = web_tool_kind(tool)
            if kind is None or self.web_role is None or request.role != self.web_role:
                raise _refused(tool.get("type"))
            if not names[kind]:
                raise WebToolsUnavailableError(
                    f"the {kind} tool is turned off for the claude-code backend "
                    f"([llm.claude_code] {kind}_tool is empty)"
                )
            tools[kind] = names[kind]
        return tools

    async def send(self, request: LLMRequest, on_text: TextSink | None = None) -> LLMResponse:
        cli_tools = self.cli_web_tools(request)  # refuses server tools before anything starts
        system = system_text(request, cli_tools)
        key = hashlib.sha256(
            json.dumps(
                [request.model, request.effort, request.max_tokens, system, sorted(cli_tools)],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        normalized = [normalized_message(message) for message in request.messages]
        conversation = self._find(key, normalized, request.messages)
        if conversation is not None:
            new = request.messages[len(conversation.seen) :]
            content = render_turns(new, conversation.tool_names)
        else:
            await self._make_room()
            conversation = await self._start(request, key, system, cli_tools)
            content = render_history(request.messages, conversation.tool_names)
        expect_tools = (
            any(not is_server_tool(tool) for tool in request.tools)
            and (request.tool_choice or {}).get("type") != "none"
        )
        # The role's own turn timeout (#409), else `[llm.claude_code] turn_timeout_seconds`.
        timeout = request.turn_timeout_seconds or self.settings.turn_timeout_seconds
        conversation.cancel_idle()
        conversation.busy = True
        try:
            turn = await asyncio.wait_for(
                conversation.turn(content, on_text, expect_tools, cli_tools), timeout
            )
        except TimeoutError as error:
            await self._discard(conversation, kill=True)
            raise LLMConnectionError(
                f"claude did not answer a {request.role} turn within {timeout:g} s"
            ) from error
        except LLMError:
            await self._discard(conversation, kill=True)
            raise
        except BaseException:  # cancelled mid-turn: the process state is unknown
            conversation.kill()
            self._forget(conversation)
            raise
        finally:
            conversation.busy = False
        event = turn.event
        if event.get("is_error") or event.get("subtype", "success") != "success":
            await self._discard(conversation, kill=True)
            raise _error_for(event)
        response = self._response(conversation, request, turn, expect_tools)
        conversation.seen = [*normalized, normalized_message(response.assistant_turn())]
        conversation.last_used = self._monotonic()
        self._schedule_idle(conversation)
        return response

    def _response(
        self,
        conversation: _Conversation,
        request: LLMRequest,
        turn: _TurnResult,
        expect_tools: bool,
    ) -> LLMResponse:
        event, text, model = turn.event, turn.text, turn.model
        reported: float | None = None
        if isinstance(event.get("total_cost_usd"), int | float):
            total = float(event["total_cost_usd"])
            reported = max(total - conversation.total_cost_usd, 0.0)
            conversation.total_cost_usd = total
        parsed = parse_tool_calls(text) if expect_tools else None
        content: list[dict[str, Any]]
        if parsed is not None:
            prose, calls = parsed
            content = [_text(prose)] if prose else []
            for call in calls:
                tool_id = TOOL_ID_PREFIX + uuid.uuid4().hex[:24]
                conversation.tool_names[tool_id] = call["name"]
                content.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": call["name"],
                        "input": call["input"],
                    }
                )
            stop_reason: str | None = "tool_use"
        else:
            content = [_text(text)] if text else []
            stop_reason = event.get("stop_reason") or "end_turn"
        return LLMResponse(
            model=model or request.model,
            stop_reason=stop_reason,
            content=[*turn.web.blocks, *content],
            usage=_usage(event, turn.web),
            billing=conversation.billing,
            reported_usd=reported,
        )

    def _find(
        self, key: str, normalized: list[str], messages: list[dict[str, Any]]
    ) -> _Conversation | None:
        """The idle live conversation `messages` continue with new user turns only, if any."""
        loop = asyncio.get_running_loop()
        for conversation in list(self._conversations):
            if conversation.loop is not loop or not conversation.alive:
                conversation.kill()
                self._forget(conversation)
                continue
            if conversation.key != key or conversation.busy:
                continue
            seen = conversation.seen
            if len(seen) >= len(normalized) or normalized[: len(seen)] != seen:
                continue
            if all(message.get("role") == "user" for message in messages[len(seen) :]):
                return conversation
        return None

    async def _start(
        self, request: LLMRequest, key: str, system: str, cli_tools: dict[str, str]
    ) -> _Conversation:
        # Absolute once, here: the CLI runs with this as its cwd and gets the system prompt file
        # by path, so a relative (or unexpanded `~`) dir would be resolved twice (issue #307).
        workdir = self.settings.workdir.expanduser().absolute()
        try:
            workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            system_file = workdir / f"system-{key[:16]}-{uuid.uuid4().hex[:8]}.md"
            system_file.write_text(system, encoding="utf-8")
        except OSError as error:
            raise LLMAPIError(f"cannot prepare the claude working directory: {error}") from error
        command = base_command(
            self.settings,
            model=request.model,
            effort=request.effort,
            system_prompt_file=system_file,
            tools=sorted(set(cli_tools.values())),
        )
        env = dict(os.environ)
        env[MAX_OUTPUT_TOKENS_ENV_VAR] = str(request.max_tokens)
        try:
            process = await self._spawn(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                limit=STREAM_LIMIT_BYTES,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as error:
            system_file.unlink(missing_ok=True)
            raise LLMAPIError(
                f"cannot run the Claude Code CLI {self.settings.executable!r}: {error} "
                "(install it, or set [llm.claude_code] executable)"
            ) from error
        conversation = _Conversation(key, process, system_file, request.model)
        conversation.last_used = self._monotonic()
        self._conversations.append(conversation)
        logger.info(
            "claude-code: started a %s conversation process (pid %s, %d live)",
            request.model,
            process.pid,
            self.process_count,
        )
        return conversation

    async def _make_room(self) -> None:
        """Close least recently used idle processes until one more fits under the limit."""
        while self.process_count >= self.settings.max_processes:
            idle = [c for c in self._conversations if c.alive and not c.busy]
            if not idle:
                logger.warning(
                    "claude-code: %d processes busy, over [llm.claude_code] max_processes",
                    self.process_count,
                )
                return
            await self._discard(min(idle, key=lambda c: c.last_used))

    def _schedule_idle(self, conversation: _Conversation) -> None:
        conversation.cancel_idle()
        conversation.idle_handle = conversation.loop.call_later(
            self.settings.idle_timeout_seconds, self._spawn_close, conversation
        )

    def _spawn_close(self, conversation: _Conversation) -> None:
        task = asyncio.ensure_future(self._close_idle(conversation))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_idle(self, conversation: _Conversation) -> None:
        if conversation.busy:
            return
        logger.info("claude-code: closing an idle conversation process")
        await self._discard(conversation)

    def _forget(self, conversation: _Conversation) -> None:
        conversation.cancel_idle()
        with contextlib.suppress(ValueError):
            self._conversations.remove(conversation)

    async def _discard(self, conversation: _Conversation, *, kill: bool = False) -> None:
        self._forget(conversation)
        if kill:
            conversation.kill()
        await conversation.close()

    async def aclose(self) -> None:
        """Close every process (e.g. at server shutdown), idle closes under way included."""
        for conversation in list(self._conversations):
            await self._discard(conversation)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def kill_all(self) -> None:
        """Kill every process at once, without awaiting (interpreter exit)."""
        for conversation in list(self._conversations):
            conversation.cancel_idle()
            if conversation.alive:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.kill(conversation.process.pid, signal.SIGTERM)
            conversation.system_file.unlink(missing_ok=True)
        self._conversations.clear()


# -- doctor ---------------------------------------------------------------------------------------


class ClaudeCodeStatus(BaseModel):
    """What `claude auth status --json` says (no secret: the account's plan, not its tokens)."""

    executable: str
    logged_in: bool
    auth_method: str | None = None
    subscription_type: str | None = None


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: str


def _run(command: list[str], timeout: float) -> _Completed:
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    return _Completed(completed.returncode, completed.stdout)


def check_claude_code(
    settings: ClaudeCodeSettings | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str], float], Any] = _run,
) -> ClaudeCodeStatus:
    """Whether the Claude Code CLI is installed and signed in: one `claude auth status --json`
    (no model call, no tokens). Raises `LLMError` when it is missing or cannot be run."""
    settings = settings or ClaudeCodeSettings()
    executable = which(settings.executable)
    if executable is None:
        raise LLMAPIError(f"{settings.executable!r} is not on PATH")
    try:
        completed = run(
            [executable, "auth", "status", "--json"], settings.auth_check_timeout_seconds
        )
    except subprocess.TimeoutExpired as error:
        raise LLMConnectionError("`claude auth status` did not answer in time") from error
    except OSError as error:
        raise LLMAPIError(f"cannot run {executable}: {error}") from error
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return ClaudeCodeStatus(
        executable=executable,
        logged_in=bool(data.get("loggedIn")) and completed.returncode == 0,
        auth_method=data.get("authMethod") if isinstance(data.get("authMethod"), str) else None,
        subscription_type=(
            data.get("subscriptionType") if isinstance(data.get("subscriptionType"), str) else None
        ),
    )
