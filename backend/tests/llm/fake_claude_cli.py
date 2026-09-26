"""A fake `claude` executable for the Claude Code backend tests: no network, no real Claude.

`install_fake_claude(directory)` writes an executable `claude` script into `directory` and returns
a `FakeClaudeCli` that scripts it. The script speaks the CLI's stream-json protocol:

- `claude auth status --json` prints `auth.json` (or `{"loggedIn": false}`) and exits.
- Otherwise it logs its argv and system prompt file to `runs.jsonl` (one line per process, with
  its pid), then for every user turn read from stdin appends `{pid, turn}` to `turns.jsonl` and
  answers with the next reply of `replies.json` (shared by every process, taken in order):
  `{"text": ..., "usage": {...}, "cost": <this turn's USD>, "stop_reason": ...}`, or
  `{"error": {"status": 429, "message": ...}}` (an error result), `{"exit": 3}` (die before
  answering) or `{"hang": true}` (never answer).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

from studentassistant.config import ClaudeCodeSettings

SCRIPT = r"""#!{python}
import fcntl, json, os, sys, time
HERE = {here!r}

def log(name, data):
    with open(os.path.join(HERE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")

def next_reply():
    path = os.path.join(HERE, "replies.json")
    with open(os.path.join(HERE, "lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        replies = json.load(open(path, encoding="utf-8"))
        if not replies:
            return {{"error": {{"message": "fake claude: no reply scripted"}}}}
        reply = replies.pop(0)
        json.dump(replies, open(path, "w", encoding="utf-8"))
        return reply

def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    try:
        print(open(os.path.join(HERE, "auth.json"), encoding="utf-8").read())
    except FileNotFoundError:
        print(json.dumps({{"loggedIn": False}}))
        sys.exit(1)
    sys.exit(0)

system = ""
if "--system-prompt-file" in args:
    # Like the real CLI: a missing prompt file is a startup error on stderr, before any input
    # is read. Paths must also be absolute (the backend passes them so; issue #307).
    prompt_file = args[args.index("--system-prompt-file") + 1]
    if not os.path.isabs(prompt_file) or not os.path.isfile(prompt_file):
        sys.stderr.write("Error: System prompt file not found: %s\n" % os.path.abspath(prompt_file))
        sys.exit(1)
    system = open(prompt_file, encoding="utf-8").read()
model = args[args.index("--model") + 1] if "--model" in args else "claude-default"
log("runs.jsonl", {{"pid": os.getpid(), "argv": args, "system": system,
                   "max_tokens": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                   "cwd": os.getcwd()}})
total = 0.0
session = "fake-session-%d" % os.getpid()
for line in sys.stdin:
    if not line.strip():
        continue
    turn = json.loads(line)
    log("turns.jsonl", {{"pid": os.getpid(), "turn": turn}})
    reply = next_reply()
    emit({{"type": "system", "subtype": "init", "session_id": session, "model": model,
          "tools": [], "apiKeySource": reply.get("api_key_source", "none")}})
    if "exit" in reply:
        sys.stderr.write("fake claude: exiting on purpose\n")
        sys.exit(reply["exit"])
    if reply.get("hang"):
        time.sleep(3600)
    if "error" in reply:
        error = reply["error"]
        emit({{"type": "result", "subtype": "error_during_execution", "is_error": True,
              "api_error_status": error.get("status"), "result": error.get("message", "boom"),
              "session_id": session, "total_cost_usd": total}})
        continue
    text = reply.get("text", "")
    half = len(text) // 2
    for piece in (text[:half], text[half:]):
        if piece:
            emit({{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0,
                  "delta": {{"type": "text_delta", "text": piece}}}}, "session_id": session}})
    usage = {{"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}}
    usage.update(reply.get("usage", {{}}))
    emit({{"type": "assistant", "message": {{"model": model, "role": "assistant",
          "content": [{{"type": "text", "text": text}}], "usage": usage}}, "session_id": session}})
    total += reply.get("cost", 0.0)
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": text,
          "stop_reason": reply.get("stop_reason", "end_turn"), "session_id": session,
          "total_cost_usd": total, "usage": usage}})
"""


class FakeClaudeCli:
    """The scripted state of one fake `claude` executable."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.executable = directory / "claude"
        (directory / "replies.json").write_text("[]", encoding="utf-8")

    def settings(self, **overrides: Any) -> ClaudeCodeSettings:
        fields: dict[str, Any] = {
            "executable": str(self.executable),
            "workdir": self.directory / "work",
            "turn_timeout_seconds": 10.0,
        }
        fields.update(overrides)
        return ClaudeCodeSettings(**fields)

    def reply(self, text: str = "", **fields: Any) -> FakeClaudeCli:
        replies = json.loads((self.directory / "replies.json").read_text(encoding="utf-8"))
        replies.append({"text": text, **fields})
        (self.directory / "replies.json").write_text(json.dumps(replies), encoding="utf-8")
        return self

    def signed_in(self, **fields: Any) -> FakeClaudeCli:
        data = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro", **fields}
        (self.directory / "auth.json").write_text(json.dumps(data), encoding="utf-8")
        return self

    def _lines(self, name: str) -> list[dict[str, Any]]:
        path = self.directory / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @property
    def runs(self) -> list[dict[str, Any]]:
        """One entry per process started: `pid`, `argv`, `system`, `max_tokens`, `cwd`."""
        return self._lines("runs.jsonl")

    @property
    def turns(self) -> list[dict[str, Any]]:
        """One entry per user turn received: `pid` and the stream-json `turn`."""
        return self._lines("turns.jsonl")

    def turn_texts(self, index: int) -> list[str]:
        """The text blocks of user turn number `index`."""
        content = self.turns[index]["turn"]["message"]["content"]
        return [block["text"] for block in content if block.get("type") == "text"]


def install_fake_claude(directory: Path) -> FakeClaudeCli:
    directory.mkdir(parents=True, exist_ok=True)
    fake = FakeClaudeCli(directory)
    fake.executable.write_text(
        SCRIPT.format(python=sys.executable, here=str(directory)), encoding="utf-8"
    )
    fake.executable.chmod(fake.executable.stat().st_mode | stat.S_IXUSR)
    return fake
