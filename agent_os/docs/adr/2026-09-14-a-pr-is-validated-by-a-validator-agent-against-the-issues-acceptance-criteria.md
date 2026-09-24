# A PR is validated by a validator agent against the issue's acceptance criteria

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
`AGENTS.md` says work lands as a PR, but the worker flow merged `--no-ff` by hand from the
interactive session, nothing ran tests on a PR (no `.github/` at all), and judging whether the
work was complete fell to the same session that wrote the brief. The owner wants doubts routed to
them, but validation done by an agent with more context that checks point by point that
everything asked for is implemented.

## Decision
- **Four headless roles, each a class in `config/agents.yaml`:** *worker* writes code on a
  branch; *validator* reviews a PR against its issue; *refiner* turns a feature or a raw issue
  into template-conformant sub-issues with a budget class; *planner* prioritizes and launches the
  other three. One driver, `scripts/agent_task.sh <role> ...`, runs any role as a one-shot;
  `worker_task.sh` keeps its name and its two slots.
- **A worker ends by pushing its branch and opening a PR** under its own identity, body
  `Closes #N`; the driver moves the issue to `status:ai-completed` and writes `worker_finished`.
- **The planner launches the validator on every `status:ai-completed` issue with an open PR.**
  The validator reads the issue's acceptance criteria and definition of done, the diff, runs
  `ruff` and the tests the issue names, and posts one PR review listing every criterion as met or
  not met with the evidence (file, test, output). *Approve* moves the issue to `status:review`;
  the human merges (`2026-08-26-the-agent-proposes-the-human-publishes.md`). *Request changes*
  makes the planner resume the worker with the review as context; it counts as an attempt under
  the relaunch cap.
- **Identities.** The validator must not be the PR's author: it runs under `roedor-planner` until
  a `roedor-validator` App exists (a browser step for the human, tracked as an issue). The
  refiner runs under `roedor-planner` for the same reason.
- **CI is the cheap floor, not the suite.** `.github/workflows/ci.yml` runs `ruff check`,
  `ruff format --check` and the tests that need no database on every PR. The full suite stays on
  this machine, run by the validator when the issue says the change is wide.

## Consequences
- The human sees a PR already reviewed criterion by criterion and only merges or answers a doubt.
- Merge stays a human act; letting the planner merge an approved PR is a separate decision.
- A validator's "not met" is evidence for the worker's next attempt, so its review must quote
  what it ran, not what it believes.
