"""Qwen Code's `--output-format stream-json` events. Today they are close enough to Claude's that
every answer is read the same way, and so this parser inherits all three methods rather than
copying them -- what differs is what the stream CARRIES, not how it is read:

- `turn_usage`: Qwen reports each turn's full context in `input_tokens` alone, and summing the
  cache counters (absent on Qwen) is right for both shapes.
- `result_usage`: the terminal `result` carries `usage.total_tokens` and no `total_cost_usd`, so
  `cost_usd` reads None (#387) -- the reason a Qwen class is cut on tokens, not dollars.
- `quota_verdict`: the stream carries no `rate_limit_event`, so the verdict reads `allowed` unless
  the terminal `result` itself says `is_error` with `api_error_status == 429` -- the same detector a
  Claude run is read with, kept unchanged here (#514 stage 1 changes no behaviour; #530 narrows the
  shared method to the rate-limit wall, not any refused status). That the guard CUTS only a Claude
  run on it is the guard's rule, not this parser's.

A Qwen release that changes its event shape overrides the method it changed, here."""

from __future__ import annotations

from agent_os.streams.claude_jsonl import ClaudeJsonlStreamParser


class QwenJsonlStreamParser(ClaudeJsonlStreamParser):
    name = "qwen_jsonl"
