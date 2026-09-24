# The agent mechanism is project-agnostic: configured, not coded

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
The guard, the planner, the drivers and the tracker CLI were written inside roedor, and roedor's
names leaked into them: the repository slug, the project-board number, the tracking epic, the
worktree paths, the identities' file names, the backend list. The owner's intent is that this is a
mechanism for running a project with agents, of which roedor is the first user, not the only one.
A second project must be able to adopt it by copying a directory and filling in one file.

## Decision
- **Everything project-specific lives in `config/agents.yaml`, under a `project:` section:**
  repository slug, project-board number and its column names, tracking epic, the label
  vocabulary, worktree roots per backend, the secrets directory for the App identities, the
  notify topic. No script under the mechanism reads a literal that belongs to one project.
- **The mechanism's scripts and their tests are grouped** so they can be lifted as one unit:
  `scripts/agent_guard.py`, `scripts/agent_lib.py`, `scripts/agent_task.sh`,
  `scripts/worker_task.sh`, `scripts/planner_task.sh`, `scripts/notify.sh`, `scripts/issues.py`
  and `tests/test_agent_*.py`, `tests/test_issues_cli.py`. They import from each other and from
  the standard library or `pyyaml`; never from `roedor/`.
- **What a project supplies, the mechanism reads from the repo, not from itself:** the role
  prompts (`RULES` blocks) reference `AGENTS.md` and the docs an issue names; they do not restate
  the project's domain rules. Project rules a role must never break (roedor's frozen `m2` stamp,
  the curated files) are cited by path from `AGENTS.md`, so a different project's `AGENTS.md`
  carries different ones with no change to the mechanism.
- **The systemd units are generated** from the `project:` section by an `install` subcommand,
  never hand-edited per machine.

## Consequences
- roedor's own values move into `config/agents.yaml` in the first correction checkpoint (#346)
  and each later checkpoint keeps the rule; a review finding a project literal in a mechanism
  script is a defect.
- Extracting the mechanism into its own repository or template later is a move, not a rewrite.
- `docs/modules/workers.md` stays the module doc here; a portable `README` for the mechanism
  itself is written when the first second project adopts it, not before.
