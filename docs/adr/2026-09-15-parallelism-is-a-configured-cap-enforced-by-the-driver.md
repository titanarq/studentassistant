# Parallelism is a configured cap enforced by the driver

- Date: 2026-09-15
- Status: accepted
- Modules: workers

## Context
Budget runs out fast (`agent_os/docs/adr/2026-09-14-agent-spend-is-tokens-not-time-and-needs-a-written-
budget.md`), and two workers editing one module's code and its `docs/modules/*.md` at the same
time produce a conflict neither of them sees until a PR lands on top of the other's uncommitted
assumptions. Until now, "how many issues run at once" was never written down anywhere but the
worktree count, and "never share a `module:` label" was a sentence in the planner's own prompt
(`scripts/planner_task.sh:102-109`, `agent_os/docs/AGENT_OS.md` §7 row (k)) -- a planner LLM mistake had no
mechanical stop, the same shape `agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-
planner-relaunches.md` already fixed for the relaunch count (#362).

## Decision
`planner.max_parallel_issues` (`config/agents.yaml`, default 1) is the configured cap on how many
issues may run at once across every backend; `agent_lib.PlannerConfig.max_parallel_issues` carries
the default and `planner-value max_parallel_issues` reads it, the same shape `relaunch_cap`
already has. `scripts/worker_task.sh <backend> start` enforces both limits itself, before writing
anything -- the issue file, the brief, the `doing` label, none of it -- the same way `resume`
already enforces `relaunch_cap`:
- **The cap.** It counts alive workers on every OTHER backend named in `project.worktrees`
  (`agent_lib.py worktree-backends`, so a project with more than two backends needs no code
  change) using the same pidfile-and-`kill -0` check `alive` already used for its own backend.
  At or above the cap, it refuses.
- **The module exclusion.** Below the cap, for each alive other backend it reads the issue number
  it recorded (`.cache/worker_<other>.issue`), fetches that issue's `module:` labels with
  `gh issue view <N> --json labels`, and compares them against the issue about to start. Any
  overlap refuses.

Either refusal writes nothing and exits non-zero. The planner no longer counts alive workers or
compares `module:` labels itself (`scripts/planner_task.sh`'s dispatch rule now says the driver
enforces both, and that a refused `start` means waiting for the next event, never retrying in the
same run) -- it only decides *which* dispatchable issue to try and on which backend, the same
split `resume`/`relaunch_cap` already drew between the driver deciding *whether the cap allows it*
and the planner deciding *whether it is worth trying*.

## Consequences
- A planner mistake can no longer dispatch a second issue past the configured cap, or one that
  collides by module with a running one -- the mechanical stop exists whether or not the LLM
  remembers the prompt sentence.
- Raising the cap above 1 (multiple issues running at once, still excluded by module) is a config
  edit, never a code change.
- `worktree-backends` makes the count project-agnostic: a project that configures a third backend
  in `project.worktrees` is covered without touching `worker_task.sh`
  (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
