# Workers run on Qwen and Claude only reviews

- Date: 2026-09-16
- Status: accepted
- Modules: workers

## Context
Two worker backends were declared and the choice between them was made by a sentence in prose, in
two places: the refiner's injected RULES (`scripts/agent_task.sh`) and the control-plane agent's
brief (`.claude/agents/control-plane.md`). Both said the same thing — `mechanical-qwen` for a
small, fully specified change, the Claude class "otherwise" — which is a rule that escalates every
issue that is not trivial to the expensive backend, and does it before any run has been measured.
Nothing in that choice rested on a measurement: the numbers in `config/agents.yaml` are declared
placeholders (the file says so; real tuning is #342), and the classification input was a human
reading the issue title.

The two quotas are not interchangeable and are not paid for the same way. The Qwen allowance is
bought outright and is the larger, cheaper window (1M model window, 400k-token budget per stage
process); Claude Code runs on a subscription window shared with the main thread, the validator, the
refiner and the planner, and that window ran out on 2026-09-15. A prose rule that sends the harder
half of the backlog to the contended quota spends it on writing code, which is the one thing the
cheap quota can also do — and starves the roles that have no alternative backend.

The effect was measurable in the backlog. On 2026-09-16, before any of this, seven of the eleven
open tasks carrying a budget class named `complex-claude`: #363, #364, #365, #367, #369, #370 and
#372. The first three of those were reclassified by hand that same morning, so the command below
now returns the four that remained, #365, #369, #370 and #372, out of twelve:

```
gh issue list --state open --limit 200 --json number,body \
  -q '.[] | select(.body | test("<!-- budget: complex-claude -->")) | .number'
```

Four issues on the contended quota, and every one of them landed there by the prose rule above,
not by a recorded cost.

## Decision
- **Every worker task runs on the Qwen backend.** The two worker classes in `config/agents.yaml`
  are `mechanical-qwen` (a small, fully specified change) and `complex-qwen` (anything else).
  `complex-claude` is renamed to `complex-qwen`: same name-shape, `backend: qwen`,
  `model: qwen3.8-max`, `max_context: 400000`, `max_cost_usd: 20.0`, `commit_warn_turns: 15`,
  `commit_cut_turns: 25`. What distinguishes the two classes is now the ceiling, not the backend —
  an underspecified brief is allowed to cost four times as much, on the same allowance.
- **Claude is kept for the roles that review and plan.** The `planner`, `validator` and `refiner`
  classes are unchanged, on `claude` / `claude-opus-5`. Qwen writes; Opus reads what it wrote.
  This is the asymmetry that matters: a review is short, needs judgement, and is the last place a
  wrong answer is caught, so it gets the scarce quota.
- **The rule that picks a class is written in both places that apply it**, in the same words: a
  worker task goes to `mechanical-qwen` when it is small and fully specified, to `complex-qwen` in
  every other case, and never to a class whose `backend:` is `claude`
  (`scripts/agent_task.sh` refiner RULES, `.claude/agents/control-plane.md`).
- **The Claude backend stays declared and unused.** `project.worktrees.claude`,
  `project.worker_apps.claude`, the `worker_task.sh claude ...` verbs and the driver's
  150k-token default for that backend are all untouched. Reactivating it costs one class in
  `config/agents.yaml` and nothing else — this decision is about where work is sent, not about
  removing a capability.
- **`qwen_fallback_eligible` stays `false` everywhere and is not touched.** With both worker
  classes on Qwen, the Claude→Qwen fallback it authorises can no longer trigger: no worker run can
  hit a Claude quota wall, because no worker run is on Claude. The field, the planner's fallback
  paragraph and the guard's quota detection all remain in place, inert, against the day a Claude
  worker class exists again. The inverse case — a Qwen worker out of quota, with no route to
  Claude — is now the unprotected one; it is deliberately **not** implemented here and is left as
  an open question for the human, not a thing an agent decides.

## Consequences
- Every issue in the funnel is dispatchable against one bought allowance; the subscription window
  is spent only by the planner, the validator, the refiner and the main thread.
- A harder task is now run by a weaker model with a bigger budget. The risk this trades into is a
  worker that needs more stages, more relaunches, or produces a PR the validator sends back. That
  is the thing to watch: relaunch counts and `request-changes` rates on `complex-qwen` issues.
- The four open issues that named the old class (#365, #369, #370, #372) carry
  `<!-- budget: complex-qwen -->` as of this decision; no issue anywhere names `complex-claude`
  outside `docs/history/` and this file.

## What would reverse this
Any one of these, and it goes back to a Claude worker class — one block in `config/agents.yaml`
plus the two prose rules:
- The Qwen allowance stops being bought, or runs out faster than the subscription window.
- Recorded runs show `complex-qwen` issues costing more end to end (relaunches plus validator
  round-trips plus the human's own time) than the same work did on Claude.
- A class of work turns out to be reliably beyond the Qwen model — which is a measurement on
  named issues, never an impression from one bad run.

## How it gets re-measured
Not by argument: **#342** is the open task that tunes the classes from recorded runs
(`.cache/worker_*.jsonl`, `.cache/<role>/runs.tsv`, and the per-issue spend under `.cache/spend/`).
The split between `mechanical-qwen` and `complex-qwen`, both ceilings, and whether a Claude worker
class earns its place back are all inputs to that task, with real numbers behind them. Until then
the numbers here remain what the file already says they are: placeholders.

## Amendment, 2026-09-22 (#514)
This decision reads as a rule about two hardcoded names, `qwen` and `claude`, because those were
the only backends `config/agents.yaml` could describe when it was written -- three parallel maps
(`project.worktrees`, `project.worker_apps`, `project.executables`) keyed by a backend's own name,
compared against string literals across the mechanism. #514 replaced the description with
`project.backends.<name>: {command, worktree, app, stream, quota}`, so nothing in the mechanism
compares a backend's name against a literal any more: a worker task class names a `backend:` key,
a role class names a `fallback.backend:` key, and the guard cuts on whatever `quota:` detector that
key declares (`none` for `qwen`, `claude_rate_limit` for `claude`, unchanged). Read this ADR's
"Every worker task runs on the Qwen backend" and "Claude is kept for the roles that review and
plan" as **which class names which configured backend today**, not as a property of the names
themselves -- the rule this decision states is about a role's own class and its declared backend
capability, and a third backend earns its place the same way `qwen` and `claude` did: one
`project.backends` entry, a stream parser only if its jsonl shape is new, and a class or a
`fallback:` naming it. Nothing else in the Decision, Consequences or reversal sections changes.
