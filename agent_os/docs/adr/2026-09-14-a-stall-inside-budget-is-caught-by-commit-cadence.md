# A stall inside budget is caught by commit cadence, not by reading what the agent says

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
A worker can be alive, reporting on time, and still be going in circles — retrying the same failing
approach, or drifting without converging — which passes any liveness check built on silence alone.
The guard cannot judge whether reasoning is sound: that would put an LLM's own output in charge of
cutting LLM spend, which is the thing #336 exists to avoid. It needs a proxy that is true by
construction, not by interpretation.

## Decision
The guard counts turns since the worker's last commit on its branch. Past a threshold
(`config/agents.yaml`, per task class — a research task commits less often than a mechanical one),
it does not cut immediately: it posts a warning comment on the issue ("N turns without a commit,
still watching") and keeps counting. A second, higher threshold cuts, freezing the work per
`agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md`.
Independently, three identical tool calls in a row (same command, same arguments, read straight off
the event stream) is treated as a stall regardless of turn count — a loop by definition, not by
inference.

## Consequences
- "The task is getting complicated" reaches the issue as a comment while it is still recoverable,
  not only as a stopped process after the fact.
- The threshold is a repo-tunable number, not a heuristic the guard invents per run.
- A task that legitimately goes long between commits (a wide search, a long read) needs its own
  class in `config/agents.yaml` with a looser threshold — never a special case in the guard's code.

## Source
Decided in conversation on 2026-09-14, issue #336.
