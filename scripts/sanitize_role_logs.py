#!/usr/bin/env python3
"""Neutralise stream events whose top-level `message` is a string, so the guard tick can read them.

Host-side workaround for titanarq/agent-os#37: `ClaudeJsonlStreamParser.turn_usage` does
`(event.get("message") or {}).get("usage")`, which assumes `message` is a dict (it is on
`assistant`/`user` events). Claude Code also emits `{"type": "system", "subtype":
"permission_denied", ..., "message": "<text>"}` when a tool call is refused, and one such line in
any role log makes `agent_guard.py tick` raise AttributeError on every fire -- no promotions, no
liveness, no quota verdicts -- until that log ages out of the quota window.

For every JSON line in `.cache/<role>/*.log` and `.cache/worker_*.jsonl` whose top-level
`message` is not an object, this renames that key to `msgtext` IN PLACE (same byte length, written
with pwrite at its offset, no truncation), so a log a run is still appending to is never
disturbed. Idempotent and silent when there is nothing to do.

The guard service runs it as an ExecStartPre (drop-in
`scripts/systemd/studentassistant-guard.service.d/sanitize-role-logs.conf`). Remove it, the
drop-in and the runbook note once agent-os#37 is fixed and the subtree is pulled.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
OLD, NEW = b'"message":', b'"msgtext":'
assert len(OLD) == len(NEW)


def fixed_offsets(line: bytes) -> list[int]:
    """Offsets (within the line) of the top-level `message` key to rename, or [] if none."""
    try:
        event = json.loads(line)
    except ValueError:
        return []
    if not isinstance(event, dict) or "message" not in event or isinstance(event["message"], dict):
        return []
    start = 0
    while (at := line.find(OLD, start)) != -1:
        candidate = line[:at] + NEW + line[at + len(OLD):]
        try:
            patched = json.loads(candidate)
        except ValueError:
            patched = None
        if isinstance(patched, dict) and "message" not in patched and "msgtext" in patched:
            return [at]
        start = at + 1
    return []


def sanitize(path: Path) -> int:
    fixes = 0
    with open(path, "r+b") as handle:
        data = handle.read()
        offset = 0
        for line in data.split(b"\n"):
            if b'"message":' in line and not line.lstrip().startswith(b'{"type":"assistant"'):
                for at in fixed_offsets(line):
                    os.pwrite(handle.fileno(), NEW, offset + at)
                    fixes += 1
            offset += len(line) + 1
    return fixes


def main() -> int:
    paths = [p for d in CACHE.iterdir() if d.is_dir() for p in d.glob("*.log")] if CACHE.is_dir() else []
    paths += list(CACHE.glob("worker_*.jsonl"))
    for path in sorted(paths):
        try:
            fixes = sanitize(path)
        except OSError as error:
            print(f"{path}: {error}", file=sys.stderr)
            continue
        if fixes:
            print(f"{path.relative_to(ROOT)}: renamed {fixes} string 'message' key(s) (agent-os#37)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
