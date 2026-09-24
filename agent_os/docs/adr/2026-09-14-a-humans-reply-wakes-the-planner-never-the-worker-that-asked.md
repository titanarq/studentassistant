# A human's reply wakes the planner, never the worker that asked

- Date: 2026-09-14
- Status: accepted
- Modules: workers, tracker

## Context
A worker is a bounded, disposable execution unit charged against a budget (see
`agent_os/docs/adr/2026-09-14-agent-spend-is-tokens-not-time-and-needs-a-written-budget.md`); leaving it
idle on a GitHub thread waiting for a human to answer would either burn its budget doing nothing or
require an open-ended exemption from the liveness rule. The decision a reply unblocks is also a
planning decision — what to relaunch, and with what — not a continuation of the worker's own
bounded task.

## Decision
A worker that needs a human decision writes `BLOCKED reason=...` to `progress.log`, posts a GitHub
comment on the issue naming the question in plain terms and mentioning the user, and ends its own
turn without waiting — its worktree is left exactly where a guard cut would leave it (see
`agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md`). The issue
is labeled `status:blocked-on-human`. However the user answers — directly on GitHub, from the
phone's app, or by opening an ad-hoc Claude session that posts the reply on their behalf — the
result is the same from the repository's point of view: a new comment on that issue. The next
monitor tick (see
`agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-never-as-a-standing-process.md`) finds
the label still set, checks for a comment newer than the one that set it, and if there is one,
clears the label and invokes the planner — never the worker that originally asked, and never
whatever session the human happened to answer through. This needs no dedicated tool: it is the same
tick that already watches budgets and quota, checking one more thing.

## Consequences
- A worker's budget is never spent waiting on a human.
- The reply is read exactly once, by the one agent with the context to act on it — the planner —
  regardless of which surface the human used to answer.
- Nothing has to be built to make this work: no dedicated "liaison" session, no webhook. The next
  tick is always at most one interval away.

## Source
Decided in conversation on 2026-09-14, issue #336.
