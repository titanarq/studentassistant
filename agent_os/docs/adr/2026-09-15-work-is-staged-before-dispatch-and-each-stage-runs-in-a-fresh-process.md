# Work is staged before dispatch, and each stage runs in a fresh process

- Date: 2026-09-15
- Status: accepted
- Modules: workers

## Context
Tokens run out mid-task more often than a whole issue actually needs a human decision, and context
inside one running process only grows: a process that has already read a stalled attempt's earlier
turns and its own dead ends carries all of it into the next turn, at growing cost, for a shrinking
share of useful work. A cut has always frozen whatever was uncommitted in a `WIP: cut by guard`
commit (`agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md`),
but relaunching, until now, meant `--resume`ing that same session -- the same swollen context that
got cut in the first place, for whatever of the task was still undone. Losing an in-progress cut
has therefore cost the whole task's context, not the sliver of work still missing.

## Decision
`## Stages` is a required section of every task/bug body (`.github/ISSUE_TEMPLATE/task.md`,
`bug.md`; `agent_lib.REQUIRED_SECTIONS`): an ordered checklist, each line one small, independently
committed deliverable. An issue that reaches `status:ready` without a well-formed one is not
dispatchable (`agent_lib.is_dispatchable`), and any `status:refine` issue missing it needs the
refiner by default -- whoever wrote the issue, human or agent, because how to fraction the work is
the refiner's own judgment call, not a size rule anyone else applies. The refiner writes the
stages inside the issue's own body (never splitting it into sub-issues for this alone -- only when
the work itself spans more than one module), on its own principles: a stage is a small unit of
work that leaves the tree green and committed, sized so a fresh process handed nothing more than
the full issue and its Context can complete it with minimal complexity; each stage names the files
it touches and how it is verified; test scaffolding comes early, documentation last; no numeric
floor or ceiling.

`scripts/worker_task.sh <backend> start` launches the backend for the *next incomplete stage
only*, closed by a commit whose subject is exactly `stage N/M: <title>`. The driver, not the
planner, chains stages: when a process exits cleanly with a new `stage N/M:` commit and the
guard's checks pass, it starts the next stage in a fresh process -- a new session, never
`--resume` of the previous one's context -- archiving the finished stage's `.jsonl` under
`.cache/spend/<issue>/` first. Only the last stage's own commit, or a failed check, produces
`worker_finished`/`worker_cut` for the planner; a process that exits without its stage's commit is
a cut, not a completed stage, whatever else it did in that process.

Budget is measured at both grains: `max_context` per stage's own process (a fresh process, a fresh
ceiling), `max_cost_usd` for the whole issue as the sum of its stage processes
(`agent_lib.cumulative_cost_usd` over the archived plus the live `.jsonl`). `worker_task.sh
<backend> resume` is the same fresh-process launch the chain itself uses, starting from the first
stage with no `stage N/M:` commit yet -- a relaunch costs only the stage that was cut, never the
ones already committed, and the relaunch cap (#362) keeps counting cut commits the same way
regardless of which stage they belong to.

## Consequences
- Running out of tokens costs the current stage, never the whole issue -- the tree is always left
  green and committed at the last completed stage, and `resume` starts clean from there.
- Every stage is its own commit: a branch's history grows longer and more granular than one commit
  per issue, which is the price of a fresh-process boundary the guard can detect mechanically off
  `git log` alone, with no state the agent has to write itself.
- A process that exits cleanly but without a `stage N/M:` commit is, by definition, a cut -- there
  is no "finished but forgot to commit" outcome left to special-case.
- The refiner now runs, by default, on every incoming task and bug regardless of how well-formed
  the rest of its body already is, because `## Stages` is the one section nothing but the refiner
  (or a human) can write -- a per-issue cost `agent_os/docs/AGENT_OS.md` §3 now accounts for.

## Source
Decided in conversation on 2026-09-15, issue #375 (parent #336, "Decision 2026-09-15 (late) --
budget first: parallelism cap and staged execution, before round 1").
