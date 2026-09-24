# The monitor and the planner run on triggers, never as a standing process

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
A daemon that is "always on" is one more thing to remember is running, one more thing
`running.sh` has to explain, and one more thing that can itself get stuck. Every event that matters
here already has a natural edge to trigger on: a worker ending, a guard cutting, an empty queue, a
human's reply landing.

## Decision
No long-lived planner or monitor process. Two triggers, both short-lived:
- **A hook fires on every worker's natural end** (`worker_task.sh`'s own `start`/`resume` wrapper,
  on the backend CLI process exiting) and invokes the monitor once.
- **A monitor tick** — a systemd timer / cron entry, not a resident process — runs
  `scripts/agent_guard.py` every few minutes. Each tick: checks every worker's `.state`/
  `progress.log` against its budget and its declared `EXPECT` (cutting and freezing per
  `agent_os/docs/adr/2026-09-14-liveness-is-judged-against-a-plan-the-worker-declares.md` and
  `agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md` when
  needed), checks quota (see
  `agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md`), and
  checks whether anything is running at all. If a cut just happened, quota just changed, or nothing
  is running and the queue is not empty, it invokes the planner (`claude -p` with the planner
  brief) and exits; the planner itself runs to a decision and exits too — it never idles waiting
  for something to change.

Before invoking the planner on a given issue, the tick checks two flags: a `status:blocked-on-human`
label on that issue (skip it — see
`agent_os/docs/adr/2026-09-14-a-humans-reply-wakes-the-planner-never-the-worker-that-asked.md`), and a
repo-wide `status:agents-paused` label on the tracking epic (#12) that stops the planner from being
invoked at all — a deliberate full stop (end of project, or the human wants the floor), never a
per-issue thing.

## Consequences
- Nothing here requires a terminal, a screen session, or a machine that never sleeps between ticks
  — only that ticks happen.
- The planner's own run is itself a worker-shaped thing and needs its own (small, cheap) budget
  class in `config/agents.yaml`, since it only plans.
- `running.sh`/`tracks.sh` gain one more thing to report: whether the monitor tick is actually
  scheduled (the timer/cron entry existing), not a live PID.

## Source
Decided in conversation on 2026-09-14, issue #336.
