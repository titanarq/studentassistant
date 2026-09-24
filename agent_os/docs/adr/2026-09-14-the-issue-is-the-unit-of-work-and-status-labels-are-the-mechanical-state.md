# The issue is the unit of work, and `status:*` labels are the mechanical state

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
The design says an agent starts from "implement #N" and finds everything it needs from the issue,
its parent, `AGENTS.md` and the docs the issue names. In practice a worker started from a brief
file in `scratchpad/` written by the interactive session, the issue only supplied the budget
line, and the worker never read `AGENTS.md`, the issue or the parent. Zero of fifty open issues
carried a budget class, "Ready for AI" existed only as a board column that no script moved, and
the shape of an issue body was a convention followed by whoever wrote it.

## Decision
- **The issue body is the brief.** A task or bug body has these sections, in this order:
  `## Objective`, `## Acceptance criteria`, `## Context` (module docs, ADRs, paths),
  `## Not included`, `## Dependencies` (`Blocked by #N` lines, or `none`), `## Definition of
  done`, and the line `<!-- budget: <class> -->`. `.github/ISSUE_TEMPLATE/` carries the template
  and `scripts/issues.py create --template task|bug` scaffolds it; `issues.py validate N` checks
  the sections, resolves the budget class against `config/agents.yaml`, and checks that every
  `Blocked by` issue is closed.
- **A worker starts from the issue.** `worker_task.sh start <issue> [extra-brief.md]`: the driver
  writes the issue body and its parent's body to `.cache/worker_<backend>.brief.md` and the
  worker's first instruction is to read `AGENTS.md`, then that file, then only the docs it names.
  An extra brief is a supplement, versioned or quoted in the issue, never the only source.
- **Mechanical state is exactly one `status:*` label per issue:** `status:refine`,
  `status:ready`, `status:doing`, `status:blocked-on-human`, `status:ai-completed`,
  `status:review`. `issues.py move N <state>` sets the label and mirrors the project column
  (Backlog, Ready for AI, In progress, AI completed, Review, Done). The guard and the planner read
  labels, never the board; the board is a mirror for the human.
- **Ready for AI** means: `issues.py validate N` passes and the label is `status:ready`. Only the
  planner or the human sets it; a worker or the refiner never does.
- **A doubt goes to the human as a comment** on the issue or PR it belongs to, with
  `status:blocked-on-human`. The human answers there. The answer wakes the planner
  (`2026-09-14-a-humans-reply-wakes-the-planner-never-the-worker-that-asked.md`), which resumes the
  same worker with the reply as context or reassigns.

## Consequences
- Any agent, headless or interactive, can start from `#N` alone; `scratchpad/brief_*.md` leaves
  the flow.
- Existing open issues without the sections are not dispatchable until refined; that is the
  refiner's job, not a reason to loosen the rule.
- `status:doing` replaces the ad-hoc use of the same label today; the board gains no new column
  beyond the six it has, `status:refine` maps to Backlog.
