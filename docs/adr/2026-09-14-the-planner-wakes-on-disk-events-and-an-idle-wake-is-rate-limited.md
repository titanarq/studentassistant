# The planner wakes on disk events, and an idle wake is rate-limited

- Date: 2026-09-14
- Status: accepted
- Modules: workers
- Supersedes the "nothing is running and the queue is not empty" trigger of
  `2026-09-14-the-monitor-and-planner-run-on-triggers-never-as-a-standing-process.md`
- Its rate limit on `idle_dispatchable` is amended by
  `2026-09-16-a-newly-dispatchable-issue-is-an-edge-not-a-condition.md`: an issue *becoming*
  dispatchable is an edge and wakes the planner on the next tick (`new_dispatchable`), while a set
  of issues *sitting* dispatchable stays the rate-limited condition this ADR describes
- Amended again by
  `2026-09-17-a-merge-is-an-edge-and-the-human-can-wake-the-planner-by-label.md`: a merged PR is
  its own edge (`pr_merged`), and a human (or the control plane, acting as them) can wake the
  planner early by label (`nudged`), both outside this ADR's rate limit

## Context
On the first day with the timer armed, the tick invoked the planner (Opus 5) 51 times in six
hours and dispatched nothing: `_queue_not_empty` counted any open issue as queued while zero of
fifty carried a budget marker (issue #345). There was no cap on planner runs, no lock against two
planners overlapping, the planner's log was truncated on every run, and a worker's natural end had
no edge to the planner at all — only the next tick's proxy condition. A monitor that pages an LLM
to conclude "nothing to do" is the opposite of a monitor that costs nothing.

## Decision
- **Neither the tick nor the exit hook invokes the planner directly.** Each writes one event file
  to `.cache/planner_events/` named `<utc-timestamp>-<kind>-<subject>` — kinds:
  `worker_finished`, `worker_cut`, `quota_changed`, `human_replied`, `idle_dispatchable` (plus
  `new_dispatchable` and the kinds added since) — and then calls `agent_guard.py wake`.
- **`wake` runs under `flock` on `.cache/planner.lock`.** If a planner is already running it exits
  at once; the events stay on disk for the running planner's end or the next wake. Otherwise, if
  there are unconsumed events, it invokes `planner_task.sh run` with the event list as the
  context, and moves the events to `.cache/planner_events/consumed/` when the run ends.
- **`idle_dispatchable` is the only rate-limited event.** The tick writes it only when nothing is
  running, at least one issue is dispatchable, and the previous idle wake is older than
  `planner.idle_wake_minutes` in `config/agents.yaml`. An issue is *dispatchable* when it is open,
  carries `status:ready`, its `<!-- budget: <class> -->` resolves, it is not
  `status:blocked-on-human`, and every issue it names as `Blocked by #N` is closed.
- **`planner.max_runs_per_day` caps every kind of wake.** Reaching it writes one ntfy page and
  stops waking until the day rolls over; events accumulate and are listed in that page.
- **Planner runs leave a trace.** Each run logs to `.cache/planner/<utc-timestamp>.log`, never
  truncated, and appends one line (timestamp, context, model, turns, cost) to
  `.cache/planner/runs.tsv`. Cost is read from the backend's `result` event.
- **The planner's dispatch rule:** at most one running issue per `module:` label at a time, and
  one issue per backend slot (`roedor-claude`, `roedor-qwen`).

## Consequences
- An empty tick costs only `gh` calls. A finished or cut worker wakes the planner within seconds
  of exiting, not within five minutes.
- Two planners can no longer act on the tracker at once; a manual `planner_task.sh run` goes
  through `wake` like everything else.
- #345 closes with this; the label-based `_queue_not_empty` is deleted, not patched.
- The dedup of repeated planner comments is mechanical (no event, no run), so the planner never
  again has to invent a "standing rule" in a comment
  (`2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md`).
