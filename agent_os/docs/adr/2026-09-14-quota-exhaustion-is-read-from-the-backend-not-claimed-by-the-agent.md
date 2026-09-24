# Quota exhaustion is read from the backend's own signal, and mechanical work falls back automatically

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
Both CLIs already say this without being asked: Claude Code emits `rate_limit_event` with
`unifiedWindows.<window>.utilization` on every turn, and a request the API refused surfaces as
`api_error_status`/`is_error` on the `result` event rather than as a clean stop. Depending on the
agent to notice and announce its own quota loss repeats the mistake named in
`agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md`: an agent
that is stuck is exactly the one that might not say so.

## Decision
The guard treats `rate_limit_event`/`api_error_status` in the event stream as the sole authority on
quota. A worker's own `QUOTA_HIT` line is logged for the planner's report but never gates anything.
When Claude's window is exhausted, the planner re-dispatches any queued task whose
`config/agents.yaml` class allows the Qwen backend, without asking; it does not touch a task class
that specifically requires Claude reasoning (the "complex analysis" class that draws on the second,
on-request subscription).

## Consequences
- The record of "why nothing is running" is exact and mechanical, never a guess reconstructed from
  prose.
- Mechanical work keeps moving on Qwen while Claude's window is closed; only Claude-specific work
  actually waits.
- The waiting case (Claude specifically needed, or Qwen also out) is exactly the case
  `agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md` pages for.

## Source
Decided in conversation on 2026-09-14, issue #336.
