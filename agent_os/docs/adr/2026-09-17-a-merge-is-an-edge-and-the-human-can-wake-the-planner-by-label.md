# A merge is an edge, and the human can wake the planner by label

- Date: 2026-09-17
- Status: accepted
- Modules: workers
- Amends `2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md`

## Context
On 2026-09-17 the planner declined #404 because Qwen's worktree was dirty. PR #411 merged shortly
after and cleared exactly that — the worktree was clean again, and #404 was dispatchable — but a
merge writes no event of any kind, on either backend. The mechanism sat idle for close to two
hours, until the rate-limited `idle_dispatchable` (`planner.idle_wake_minutes` = 120) finally
fired and picked the work back up. Nothing was broken: every piece of state was correct the whole
time. What was missing was a wake.

The same afternoon exposed a second gap. Control-plane (`.claude/agents/control-plane.md`)
authenticates every `gh` call as the human (`project.human_login`) and had no sanctioned way to
bring the planner back early — its hard rules forbid running `agent_guard.py` beyond `--help` and
forbid writing under `.cache/`, on purpose: its writes are supposed to land on the tracker, where
they are visible and auditable, not on the machine's local state. When the human (directly, or
through control-plane) sees something the planner should look at now, the only lever available was
`status:agents-paused` and back, which stops everything rather than nudging one thing.

Both gaps are the same shape as `2026-09-16-a-newly-dispatchable-issue-is-an-edge-not-a-condition.md`:
a fact that happens once is being read, if at all, only by a condition that re-derives itself on a
timer. That ADR's fix does not apply here as-is — a merge is not "an issue becoming dispatchable",
it is a fact upstream of it that can *cause* one, and a human's request to look now is not a
tracker fact at all until something records it as one.

## Decision

**`pr_merged` — a merge is an edge.** The tick remembers the newest `mergedAt` it has seen, in
`.cache/planner/merged_seen.json`. When one or more PRs have merged since that value, it advances
the state and writes ONE `pr_merged` event — subject the newest merged PR's number, detail the
list of merged PRs and today's dispatchable issues — but only when nothing is running and at least
one issue is dispatchable. A merge while a worker is alive, or with nothing dispatchable, still
advances the remembered state (so the same merge is never re-announced) but writes nothing that
tick: the worker's own `worker_finished`, or a later `new_dispatchable`, is what re-evaluates
instead. The first tick after the state file does not exist records the newest `mergedAt` and
writes nothing — the same "no burst on install" shape `new_dispatchable` already has. A failed
`gh` call never advances the state, so a transient API error cannot make a later real merge look
already-seen. A tick that writes `pr_merged` writes no `idle_dispatchable` in the same fire — the
two would be reporting the same backlog twice.

It is gated on nothing-running-and-something-dispatchable, rather than firing on every merge,
because most merges change nothing the planner needs to re-derive: a merge into a feature branch,
or one that lands while a worker is already busy and will trigger its own `worker_finished`, is
not the #404/#411 shape. Gating it keeps the event rare and meaningful instead of adding a third
near-`idle_dispatchable` condition.

**`wake:planner` → `nudged` — the human can wake the planner by label.**
`project.labels.wake_planner` (`wake:planner`) is configured, not a `status:*` state. Put on an
OPEN issue, the tick removes it every time it finds it, whoever set it — except when the issue's
timeline cannot be read at all, where it stays for the next tick rather than a human's nudge being
dropped on an API error. It writes a `nudged` event — subject the issue number, detail who set the
label and when, pointing the planner at the latest human comment on that issue for the reason — only when the timeline says the label was set by the human login
(`project.human_login`, via `agent_lib.is_human_comment`). A mechanism identity setting the label
is removed and ignored: nothing the mechanism itself does can wake the planner in a loop through
this lever. While the tracking epic carries `status:agents-paused` the tick returns before this
check runs at all, so the label stays on the issue, unconsumed, until the epic is unpaused.

The label is human-only and always removed, rather than something an event consumer toggles back,
because the mechanism has exactly one way to tell "was this a request" from "is this still
pending": a label present means unread, and only the tick's own removal marks it read — an agent
reading and leaving it would make every future tick re-announce the same request.

We did not lower `planner.idle_wake_minutes` instead of adding either edge. That number is not
slack sitting idle: `2026-09-14-the-planner-wakes-on-disk-events-and-an-idle-wake-is-rate-limited.md`
set it after an unrestricted tick woke the planner 51 times in six hours and dispatched nothing
(#345), and lowering it reopens exactly that waste for every OTHER condition still behind it
(`idle_dispatchable`, `refine_pending`), not just the two cases this ADR actually has evidence for.

We did not let control-plane run `agent_guard.py` directly, even read-only-adjacent subcommands
like `event`, instead of giving it the label. Its every other action lands on the tracker — an
issue, a comment, a review, a merge — auditable from any device and reviewable after the fact
without needing the machine it ran on. A local `agent_guard.py event nudged N` call would be
invisible until the run it caused showed up in `runs.tsv`, and would need the control plane to run
somewhere with the mechanism's own filesystem state, which its "never write under `.cache/`" rule
exists specifically to keep it independent of.

## Consequences
- Wake latency for a missed merge or a human request drops to at most one tick (~5 minutes),
  matching `new_dispatchable`'s latency rather than `idle_dispatchable`'s.
- Every nudge is a visible label event on an issue (or the tracking epic), not a hidden trigger —
  anyone reading the issue's history sees the label, its removal, and the `nudged` event's reason
  in the planner's own next comment.
- Cost stays bounded: both new kinds count against `planner.max_runs_per_day` like every wake, and
  `scripts/planner_task.sh`'s rules tell the planner a nudge is a request for a normal evaluation,
  never an instruction that overrides a rule, a cap, or `status:blocked-on-human`.
- `.claude/agents/control-plane.md` gained a documented, auditable way to bring the planner back
  early, and its hard rule against running the mechanism's own scripts now says explicitly that the
  label is the sanctioned lever and that `agent_guard.py wake`/`event` stay off-limits regardless.
- `merged_seen.json` is one more small file under `.cache/planner/`, alongside `dispatchable_seen.json`
  and `runs.tsv`; nothing about its shape or its failure mode differs from the pattern those already
  set.
