# An agent run carries a written token budget, and a run without one does not start

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
Workers today have a context ceiling read from `usage_report` in `worker_task.sh`, but nothing
stops a dispatch that never had a number attached to it, and nothing distinguishes a worker that is
thinking from one that is idle-waiting on an external process. The subscriptions this runs on are
asymmetric and finite — a small Claude Code plan, occasional access to a second plan for complex
work that has to be requested, and a larger Qwen allowance — so what a task is allowed to spend has
to be decided before it starts, not discovered afterward.

## Decision
Spend is measured in tokens and dollars read from each backend's own usage stream
(`cache_read_input_tokens` + `cache_creation_input_tokens` + `input_tokens` per turn,
`total_cost_usd` where the backend reports it — `usage_report` in `worker_task.sh` already computes
this), never in wall-clock time. An issue is dispatchable only once its body carries a
`<!-- budget: ... -->` block (same convention as the existing `<!-- key: -->` line) naming a task
class defined in `config/agents.yaml`; the class resolves to a backend, a model, a max context, and
a max cost/tokens. `worker_task.sh start` refuses an issue with no resolvable budget.

## Consequences
- `config/agents.yaml` becomes the one place task classes and their ceilings are tuned — not a
  flag on the command line, not a number improvised in a brief.
- A dispatch with no budget is a bug in the queue, not a judgment call for whoever is dispatching.
- Wall-clock is freed to mean only one thing: how long a worker has gone without a word — a
  separate question, decided in
  `agent_os/docs/adr/2026-09-14-liveness-is-judged-against-a-plan-the-worker-declares.md`.

## Source
Decided in conversation on 2026-09-14, issue #336.


## Amendment, 2026-09-16 (#387)

The Decision above resolved a class to "a max cost/tokens", but only the dollar half was ever
written as a ceiling and only the dollar half was ever cut on. On the backend every worker now runs
on that made the ceiling inert: Qwen's terminal `result` event carries
`usage.{input_tokens,output_tokens,cache_read_input_tokens,total_tokens}` and no `total_cost_usd`
(`agent_os/docs/adr/2026-09-16-workers-run-on-qwen-and-claude-only-reviews.md`), so `cumulative_cost_usd`
summed `0.0` for the whole life of an issue, `budget_exceeded`'s dollar test never fired, and the
only ceiling that bit was `max_context` — per stage process, which bounds one stage and not the
total. Measured, not inferred: `cumulative-cost` prints `0.0000` over every archived stage log of
#363, #390 and #387, while `cumulative-tokens` over the same files prints 49,526,715, 14,405,623 and
14,265,855.

The ceiling is therefore in tokens as well. A class carries a required `max_total_tokens`, measured
over the same scope as the dollars — the whole issue, summed over its stage processes' archived plus
live `.jsonl` logs — and passing it gives the same `reason=budget` outcome in both places that
check: the guard's tick cuts the live run, and the driver's stage gate refuses to chain the next
stage. One listing feeds both sums (`agent_guard.issue_stage_logs`, `worker_task.sh`
`issue_spend_logs`), so the dollar ceiling and the token ceiling can never be measured over a
different set of stages, and one function reads a run's token total (`agent_lib.result_total_tokens`:
`usage.total_tokens`, falling back to the four counters summed when it is absent) behind both the
single-stage report and the issue-wide sum. What the issue has spent in tokens is printed beside its
ceiling by `worker_task.sh <backend> status`, so the figure a cut is judged on is one a human can
read too.

Dollars remain a second ceiling, not a replaced one: the three Claude roles — planner, validator,
refiner — do report `total_cost_usd`, and a second ceiling costs nothing.

Neither unit is imputed, and the honesty costs a known undercount. A stage whose own terminal
`result` carries no tokens contributes 0: over #363's ten stage logs, three have no terminal
`result` at all (1, 30 and 46 turns of real work, uncounted) and a fourth ends in
`subtype=error_during_execution` whose `usage` is `{"input_tokens": 0, "output_tokens": 0}` despite
12 turns, so four of the ten count 0 and 49,526,715 is a floor rather than a total. That is stated
in the code's own docstrings and tracked as a gap (`agent_os/docs/AGENT_OS.md` §7 row (v)) instead of being
filled with a per-turn sum no terminal event corroborates — the alternative would undercount in
exactly the case a ceiling exists for, since a runaway is what produces cut and errored stages.

The five `max_total_tokens` values are placeholders resting on one measurement, exactly as the
dollar ones are: `mechanical-qwen` 80,000,000 is 1.6× the worst case measured (#363's 49,526,715,
itself a floor), `complex-qwen` 150,000,000 has no measurement of its class yet, and the three Claude
roles carry 2,000,000 / 10,000,000 / 10,000,000. #342 calibrates all five against recorded runs.
