"""The one interface every backend's event-stream parser satisfies, and the values it returns.

A backend's CLI writes one JSON event per line; what differs between CLIs is the SHAPE of those
events, never what the mechanism wants to know from them. That is three questions, and a parser is
exactly the three methods that answer them (#514):

- `turn_usage` -- how big each turn was, how many there were, what they wrote, and the run's
  terminal `result` event if it reached one (the guard's context ceiling and stall check).
- `result_usage` -- what the terminal `result` event reports for the run as a whole: its dollars,
  when the backend reports any, and its tokens (the issue-wide ceilings).
- `quota_verdict` -- whether the backend itself refused the run on quota, read off the stream and
  never off the agent's own words
  (agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md).

Kept apart from the registry in `__init__.py` so each parser module can import these types without
importing the registry that imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

QuotaStatus = Literal["allowed", "exhausted"]


@dataclass
class UsageSummary:
    session_id: str
    turns: int
    context: int
    output_tokens: int
    result: dict | None


@dataclass(frozen=True)
class ResultUsage:
    """What one run's terminal `result` event reports for the run as a whole. `cost_usd` is None
    when the backend reports no dollar figure at all -- Qwen's `result` carries none (#387) -- which
    is not the same as a run that cost nothing."""

    cost_usd: float | None
    total_tokens: int
    event: dict


@dataclass(frozen=True)
class StreamQuotaVerdict:
    """`reason` is the backend's own words for the refusal, None exactly when `status` is
    `allowed`."""

    status: QuotaStatus
    reason: str | None


@runtime_checkable
class StreamParser(Protocol):
    name: str

    def turn_usage(self, events: list[dict]) -> UsageSummary: ...

    def result_usage(self, events: list[dict]) -> ResultUsage | None: ...

    def quota_verdict(self, events: list[dict]) -> StreamQuotaVerdict: ...
