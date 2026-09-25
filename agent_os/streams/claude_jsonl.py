"""Claude Code's `--output-format stream-json` events: `assistant` turns carrying
`message.usage`, `rate_limit_event`s carrying the quota window's state on every turn, and one
terminal `result` with `total_cost_usd`, `usage` and, on a refused run, `api_error_status`."""

from __future__ import annotations

from agent_os.streams.interface import (
    ResultUsage,
    StreamQuotaVerdict,
    UsageSummary,
    event_message,
)


def turn_context_tokens(usage: dict) -> int:
    """Context size of one turn = everything it read: uncached input plus what came from the
    prompt cache. A warm turn reports `input_tokens: 2, cache_read_input_tokens: 18919`; reading
    `input_tokens` alone would call a 19k-token turn a 2-token one. Qwen reports the full figure in
    `input_tokens` alone, so summing is right for both."""
    return sum(
        usage.get(k) or 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def result_total_tokens(result: dict) -> int:
    """Tokens one run's terminal `result` event reports for the run as a whole. Qwen puts the
    figure in `usage.total_tokens`, and its cache reads are already inside `input_tokens`: the
    first archived stage log of #363 reports input 30,215,389 + output 196,945 = `total_tokens`
    30,412,334, with `cache_read_input_tokens` 29,875,819 a part of that input and not an addition
    to it. A log that reports no `total_tokens` falls back to the four counters summed, which is
    the figure `usage-report` has always printed. ONE implementation for both, because what a
    single stage's report shows and what the issue-wide ceiling is measured with are the same
    number at two scopes (#387)."""
    usage = result.get("usage") or {}
    return usage.get("total_tokens") or (
        turn_context_tokens(usage) + (usage.get("output_tokens") or 0)
    )


class ClaudeJsonlStreamParser:
    name = "claude_jsonl"

    def turn_usage(self, events: list[dict]) -> UsageSummary:
        context = out = turns = 0
        session_id = ""
        result = None
        for event in events:
            session_id = event.get("session_id") or session_id
            usage = event_message(event).get("usage") or event.get("usage") or {}
            size = turn_context_tokens(usage)
            if event.get("type") == "assistant" and size:
                context = max(context, size)
                turns += 1
            out += usage.get("output_tokens") or 0
            if event.get("type") == "result":
                result = event
        return UsageSummary(session_id, turns, context, out, result)

    def result_usage(self, events: list[dict]) -> ResultUsage | None:
        """None when the run never reached a terminal `result` -- killed before finishing."""
        result = self.turn_usage(events).result
        if not result:
            return None
        return ResultUsage(result.get("total_cost_usd"), result_total_tokens(result), result)

    def quota_verdict(self, events: list[dict]) -> StreamQuotaVerdict:
        """Authority order per the ADR: `rate_limit_event` first (it is the backend saying so on
        every turn), the terminal `result`'s `api_error_status == 429` as a fallback for a run that
        ended before a `rate_limit_event` could report the rejection. 429 is the rate-limit wall;
        any other refused status (5xx, other 4xx) is a transport or server failure, not a spent
        quota, and reads `allowed` here -- `usage_failed` still counts that run as failed."""
        for event in events:
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    windows = info.get("unifiedWindows") or {}
                    rejected = [w for w, v in windows.items() if v] or ["unknown"]
                    return StreamQuotaVerdict(
                        "exhausted", f"rate_limit_event status=rejected window={rejected[0]}"
                    )
        summary = self.turn_usage(events)
        if (
            summary.result
            and summary.result.get("is_error")
            and summary.result.get("api_error_status") == 429
        ):
            return StreamQuotaVerdict("exhausted", "result api_error_status=429")
        return StreamQuotaVerdict("allowed", None)
