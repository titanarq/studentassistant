# The driver writes the state the monitor needs; the agent's own state is cooperative, and its absence is the signal

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
On 2026-09-13 a subagent ended its turn mid-refactor and left the tree broken without saying so.
Anything that depends on a struggling or hung agent choosing to report it is exactly the mechanism
that already failed once. The two things a monitor needs to know split cleanly by who can actually
know them without the agent's cooperation.

## Decision
Two files per worker, both under `.cache/`:
- `.cache/worker_<backend>.state` — one current line, overwritten, written only by
  `worker_task.sh` itself: `STARTED`, `RESUMED after=<quota|guard_cut|manual>`, `DONE`,
  `CUT_BY_GUARD reason=<stall|budget|quota>`. Every one of these is knowable from the wrapper's own
  bookkeeping, with zero cooperation from the agent — a hung agent cannot corrupt or block this file.
- `progress.log` (already in use, per-worktree) — appended lines from the agent itself, a closed
  vocabulary: `HEARTBEAT` (the `EXPECT` declaration, see
  `agent_os/docs/adr/2026-09-14-liveness-is-judged-against-a-plan-the-worker-declares.md`), `QUOTA_HIT
  backend=<b>` (best-effort; the guard's own detection from the backend's usage stream is
  authoritative — see
  `agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md`),
  `BLOCKED reason=<text>`.

The guard and the planner read `.state` for ground truth and `progress.log` for narrative context.
A worker going silent past its declared `EXPECT` is not a missing report to go chase — it is the
signal itself.

## Consequences
- Nothing the monitor relies on for a go/no-go decision depends on an agent that might be the one
  malfunctioning.
- `progress.log`'s vocabulary is fixed, so the planner can read it without an LLM pass over free
  text.

## Source
Decided in conversation on 2026-09-14, issue #336. History: the mid-refactor stop of 2026-09-13.
