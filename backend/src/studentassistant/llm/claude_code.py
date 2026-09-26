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
The API's server tools (web search / fetch) are not available and are refused.

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

from studentassistant.config import ClaudeCodeSettings
from studentassistant.llm.errors import (
    LLMAPIError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
)
from studentassistant.llm.transport import TextSink
from studentassistant.llm.types import Billing, LLMRequest, LLMResponse, Usage

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
    settings: ClaudeCodeSettings, *, model: str, effort: str, system_prompt_file: Path
) -> list[str]:
    """The `claude` command line of one conversation process."""
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
        "",
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
        else:
            rendered.append(_text(json.dumps(block, ensure_ascii=False)))
    return rendered


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


def tools_instruction(tools: Sequence[dict[str, Any]], tool_choice: dict[str, Any] | None) -> str:
    """The system-prompt section describing `tools`; empty without tools.

    Raises `LLMAPIError` for a server tool (web search / fetch), which only the API runs.
    """
    if not tools:
        return ""
    parts = [_TOOLS_INSTRUCTION]
    for tool in tools:
        kind = tool.get("type")
        if kind not in (None, "custom"):
            raise LLMAPIError(
                f"the server tool {kind!r} needs the Anthropic API: it is not available with "
                "the claude-code backend ([llm] backend)"
            )
        schema = json.dumps(tool.get("input_schema", {}), ensure_ascii=False, indent=1)
        parts.append(
            f"## {tool.get('name')}\n\n{tool.get('description', '')}\n\n"
            f"Input schema (JSON Schema):\n```json\n{schema}\n```\n"
        )
    if tool_choice is not None and tool_choice.get("type") == "none":
        parts.append("Do not call any tool in your next reply.\n")
    return "\n".join(parts)


def system_text(request: LLMRequest) -> str:
    """The whole system prompt of the request's conversation: its blocks, then its tools."""
    text = "\n\n".join(
        block.get("text", "") for block in request.system if block.get("type", "text") == "text"
    )
    tools = tools_instruction(request.tools, request.tool_choice)
    return "\n\n".join(part for part in (text, tools) if part)


_FENCE_OPENER = re.compile(r"```[a-zA-Z]*\s*$")
_TOOL_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    """`(prose before the call, calls)` when `text` is a tool-call reply, else `None`.

    A call whose JSON is malformed keeps its raw input as a string, so the caller reports the
    exact JSON error (as `structured` does) instead of "you did not call the tool".
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
        data = json.loads(raw)
    except json.JSONDecodeError:
        names = _TOOL_NAME.findall(raw)
        if not names:
            return None
        return prose, [{"name": names[0], "input": raw}]
    if not isinstance(data, dict) or not isinstance(data.get("tool_calls"), list):
        return None
    calls = [
        {"name": call["name"], "input": call.get("input", {})}
        for call in data["tool_calls"]
        if isinstance(call, dict) and isinstance(call.get("name"), str)
    ]
    return (prose, calls) if calls else None


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
            raise LLMConnectionError("the claude process is not accepting input")
        try:
            stdin.write(line.encode("utf-8") + b"\n")
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise LLMConnectionError(f"the claude process closed its input: {error}") from error

    async def turn(
        self, content: list[dict[str, Any]], on_text: TextSink | None, expect_tools: bool
    ) -> tuple[dict[str, Any], str, str | None]:
        """Send one user turn; return its `result` event, the answer's text and its model."""
        await self._write(content)
        stdout = self.process.stdout
        assert stdout is not None
        sink = _TextStream(on_text, expect_tools)
        texts: list[str] = []
        model: str | None = None
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as error:
                raise LLMConnectionError(f"unreadable claude output: {error}") from error
            if not raw:
                await self._wait_exit()
                raise LLMConnectionError(
                    f"the claude process exited (code {self.process.returncode}) before "
                    f"answering: {self.stderr_summary() or 'no error output'}"
                )
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
            elif kind == "result":
                text = "".join(texts)
                if not text and isinstance(event.get("result"), str):
                    text = event["result"]
                if not event.get("is_error") and event.get("subtype", "success") == "success":
                    await sink.finish(text, is_tool_call=expect_tools and _is_call(text))
                return event, text, model

    async def _wait_exit(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), CLOSE_GRACE_SECONDS)

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


class _TextStream:
    """Passes text deltas on, except a reply that looks like a tool call (held, then dropped)."""

    def __init__(self, on_text: TextSink | None, expect_tools: bool) -> None:
        self.on_text = on_text
        self.expect_tools = expect_tools
        self.pending = ""
        self.mode: str | None = None  # "stream" or "hold" once the first visible char is seen

    async def feed(self, delta: str) -> None:
        if self.on_text is None or not delta:
            return
        if self.mode == "stream":
            await self.on_text(delta)
            return
        self.pending += delta
        if self.mode is None:
            visible = self.pending.lstrip()
            if not visible:
                return
            self.mode = "hold" if self.expect_tools and visible[0] in "{`" else "stream"
            if self.mode == "stream":
                pending, self.pending = self.pending, ""
                await self.on_text(pending)

    async def finish(self, text: str, *, is_tool_call: bool) -> None:
        if self.on_text is None or self.mode == "stream" or is_tool_call:
            return
        if text:  # held back (or never streamed): the whole answer at once
            await self.on_text(text)


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


def _usage(event: dict[str, Any]) -> Usage:
    usage = event.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        web_search_requests=int(server.get("web_search_requests") or 0),
        web_fetch_requests=int(server.get("web_fetch_requests") or 0),
    )


_live_transports: weakref.WeakSet[ClaudeCodeTransport] = weakref.WeakSet()


@atexit.register
def _kill_all_at_exit() -> None:  # pragma: no cover - interpreter shutdown
    for transport in list(_live_transports):
        transport.kill_all()


class ClaudeCodeTransport:
    """`Transport` over long-lived headless `claude` processes, one per conversation.

    One instance should serve the whole backend process (the server passes one to every feature),
    so conversations are found again across requests. `spawn` replaces
    `asyncio.create_subprocess_exec` and `monotonic` the clock (tests).
    """

    def __init__(
        self,
        settings: ClaudeCodeSettings | None = None,
        *,
        spawn: Spawn | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or ClaudeCodeSettings()
        self._spawn: Spawn = spawn or asyncio.create_subprocess_exec
        self._monotonic = monotonic
        self._conversations: list[_Conversation] = []
        self._closing: set[asyncio.Future[None]] = set()
        _live_transports.add(self)

    @property
    def process_count(self) -> int:
        """How many `claude` processes are live (for tests and diagnostics)."""
        return sum(1 for conversation in self._conversations if conversation.alive)

    async def send(self, request: LLMRequest, on_text: TextSink | None = None) -> LLMResponse:
        system = system_text(request)  # refuses server tools before anything starts
        key = hashlib.sha256(
            json.dumps(
                [request.model, request.effort, request.max_tokens, system], ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        normalized = [normalized_message(message) for message in request.messages]
        conversation = self._find(key, normalized, request.messages)
        if conversation is not None:
            new = request.messages[len(conversation.seen) :]
            content = render_turns(new, conversation.tool_names)
        else:
            await self._make_room()
            conversation = await self._start(request, key, system)
            content = render_history(request.messages, conversation.tool_names)
        expect_tools = bool(request.tools) and (request.tool_choice or {}).get("type") != "none"
        conversation.cancel_idle()
        conversation.busy = True
        try:
            event, text, model = await asyncio.wait_for(
                conversation.turn(content, on_text, expect_tools),
                self.settings.turn_timeout_seconds,
            )
        except TimeoutError as error:
            await self._discard(conversation, kill=True)
            raise LLMConnectionError(
                f"claude did not answer within {self.settings.turn_timeout_seconds:g} s"
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
        if event.get("is_error") or event.get("subtype", "success") != "success":
            await self._discard(conversation, kill=True)
            raise _error_for(event)
        response = self._response(conversation, request, event, text, model, expect_tools)
        conversation.seen = [*normalized, normalized_message(response.assistant_turn())]
        conversation.last_used = self._monotonic()
        self._schedule_idle(conversation)
        return response

    def _response(
        self,
        conversation: _Conversation,
        request: LLMRequest,
        event: dict[str, Any],
        text: str,
        model: str | None,
        expect_tools: bool,
    ) -> LLMResponse:
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
            content=content,
            usage=_usage(event),
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

    async def _start(self, request: LLMRequest, key: str, system: str) -> _Conversation:
        workdir = self.settings.workdir
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
