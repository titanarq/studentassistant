# A newly dispatchable issue is an edge, not a condition

- Date: 2026-09-16
- Status: accepted
- Modules: workers
- Amends `2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md`, whose
  `idle_dispatchable` rate limit covered both halves of "something is dispatchable"

## Context
The 2026-09-14 decision put `idle_dispatchable` behind `planner.idle_wake_minutes` because an
unrestricted tick had woken the planner 51 times in six hours and dispatched nothing (#345). What
was wasted there was a repeated wake for the **same standing condition**: the same backlog, still
sitting there, re-derived every five minutes. The rate limit is the right answer to that and
nothing here weakens it.

But the same event kind also carried a different fact. An issue *becoming* dispatchable — a human
labelling it `status:ready`, the guard's `promote-refined` moving a refined child, a blocker
closing, a cut run put back — is not a standing condition; it is an edge, and it is exactly the
kind of thing the same ADR says the planner should be woken by ("the planner is woken by edges,
never by a condition someone re-derives every tick"). Conflated with the condition, it inherited
the condition's rate limit: work that became ready one minute after an idle wake waited a full
`idle_wake_minutes` (today 120) before anything looked at it, for no reason anyone had decided.

## Decision
- **The guard persists the dispatchable set between ticks**, as
  `<planner cache dir>/dispatchable_seen.json` (so `PLANNER_CACHE_DIR` relocates it with the run
  log and `runs.tsv`), and rewrites it on every tick.
- **A tick whose set gained a number the previous tick did not have writes `new_dispatchable`**,
  a new event kind naming the issues that were gained. It is **not** subject to
  `planner.idle_wake_minutes`: an edge happens once, so rate-limiting it is rate-limiting the
  fact itself.
- **A non-empty set that gained nothing keeps today's behaviour exactly**: at most one
  `idle_dispatchable` per `planner.idle_wake_minutes`, same log line, same event.
- **The first tick with no state file records everything as already-seen** and writes no edge, so
  installing this does not fire a burst for a backlog that was already waiting. An unreadable
  state file reads the same way, never as an empty set.
- **An issue that leaves the set and returns counts as new when it returns.** The set is
  rewritten, not accumulated: a cut run put back to `status:ready` is an edge worth a wake.
- **The set is scanned on every tick, whether or not a worker is alive.** How much may run at once
  is `planner.max_parallel_issues`, enforced by `worker_task.sh start` itself
  (`2026-09-15-parallelism-is-a-configured-cap-enforced-by-the-driver.md`), and the guard does not
  second-guess a configured cap by deciding for itself that an edge cannot matter yet.
- **`planner.max_runs_per_day` and `planner.max_parallel_issues` still apply unchanged.** This
  changes *when* the planner is woken, never how much may run: the day's ceiling and the dispatch
  cap are where they were.
- The state lives in the guard and not in a hook inside `issues.py move`, because a `status:ready`
  label added by hand in the GitHub web UI never goes through `move`. Diffing the set covers both.

## Consequences
- Dispatch latency for newly-ready work drops from up to `idle_wake_minutes` (120) to one tick
  (5 minutes).
- The tick makes its two `gh` calls on every fire rather than only on an idle one. That is the
  price of seeing the edge while a worker is alive, and it is `gh`, not a model turn.
- The planner may be woken by an edge it cannot act on yet, when a worker is already running and
  `max_parallel_issues` is 1: `start` refuses, the run ends, and the cost is one planner run.
- **What would revert this**: `.cache/planner/runs.tsv` showing `new_dispatchable` runs that
  dispatched nothing because a slot was never free. The revert is not the rate limit — it is
  gating the edge on "no worker alive", the same gate `idle_dispatchable` already has. Restoring
  the rate limit on the edge would be the wrong fix: it would hide the fact rather than the run.
