# The refiner runs unattended only after a human reviewed its dry run

- Date: 2026-09-15
- Status: accepted
- Modules: workers

## Context
The backlog carries raw feature issues and small tasks/bugs with no template shape, no budget
class, and no way for a worker to start from them
(agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-state.md).
Issue #349's own acceptance criteria name the shape: a REFINER role that turns a raw issue into
template-conformant sub-issues with a budget class, or rewrites a small task/bug's body in place,
and a dry run on three real backlog issues reviewed by the human before the planner is allowed to
run it unattended. The risk this decision is built around is a refiner that writes something a
worker then tries to execute before anyone has checked it reads like a real brief.

## Decision
- **Two shapes, decided by the refiner itself.** A task or bug that is already one reviewable pull
  request in one module: rewrite its body in place, having first posted the ORIGINAL body verbatim
  as a comment so nothing is lost. A feature, or a task/bug spanning more than one module or more
  than one reviewable pull request: split into sub-issues, each moved to `status:refine`, and the
  original never has its own body rewritten (a feature has no template shape). Once the split
  exists, the original's own `status:refine` label comes off — read via
  `agent_lib.py project-value labels.refine`, never a literal in the script — so the same issue is
  never handed to the refiner a second time.
- **The refiner never sets `status:ready`.** Every issue it writes or rewrites stays
  `status:refine`; promotion to `status:ready` is either a human action or the mechanical rule
  below, never the refiner's own call
  (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
  state.md).
- **Mechanical promotion is opt-in per feature.** A human labels a FEATURE `auto-ready` to say its
  refined children may skip a second human look. `agent_lib.promotable_to_ready` is the pure
  predicate (open, `status:refine`, not blocked, `validate_issue_body` finds nothing wrong, AND the
  parent carries `auto-ready`); an issue with no parent is never promotable this way, because there
  is no feature above it to have opted in — the human promotes it by hand.
  `scripts/agent_guard.py promote-refined` is the mechanical action: for every open `status:refine`
  issue, fetch its parent's labels and move the promotable ones to `status:ready`.
- **The unattended trigger is a new, gated event.** `refine_pending` is `idle_dispatchable`'s
  sibling for the refiner (`agent_lib.needs_refinement`: open, `status:refine`, not blocked, body
  fails `validate_issue_body`), written by the same tick, at the same
  `planner.idle_wake_minutes` rate limit — but ONLY when `planner.refiner_unattended` in
  `config/agents.yaml` is `true`. It ships `false`, with a comment saying it stays that way until
  the human has reviewed the refiner's dry run on the three issues #349 names. Flipping it is a
  human decision, never something a checkpoint does on its own.
- **Loop safety.** The refiner's one summary comment on the original issue starts with the fixed
  marker `<!-- refiner-summary -->`. The planner checks for that marker (`gh issue view N --json
  comments`) before launching the refiner on an event naming that issue again — a `refine_pending`
  event is a condition, not a promise the issue is untouched, and an issue that already carries a
  summary but is still not dispatchable is a doubt for the human, not something to retry silently.
  A written issue that still fails `issues.py validate` after the refiner's own pass is the other
  half of loop safety: the refiner's summary says so and moves the ORIGINAL to
  `status:blocked-on-human` with a `@__HUMAN_LOGIN__` mention, rather than leaving a silently broken
  issue in the queue.
- **A manual, reviewed run never wakes the planner.** `scripts/agent_task.sh <role> ... --no-wake`
  writes the same log and `runs.tsv` row as any run, but writes neither the `<role>_finished` event
  nor calls `agent_guard.py wake` — exactly what the three-issue dry run needs: a human watches the
  result before anything downstream reacts to it.

## Consequences
- The backlog becomes dispatchable without a human hand-rewriting every issue, but only after a
  human has read three real examples of the refiner's own judgment — the same "trust once
  demonstrated" shape the validator and the worker mechanism already follow.
- `promote-refined` and `refine_pending` are pure-predicate-backed (`needs_refinement`,
  `promotable_to_ready` in `scripts/agent_lib.py`), so the decision of *what* is promotable is
  tested without a live agent; only the `gh`-backed selection and the one mutating action need
  mocking.
- A feature that never gets `auto-ready` keeps every one of its children under human review before
  dispatch — opting a project's more mechanical modules into unattended promotion is a label, not a
  code change.
- `needs_refinement` checks only the STRUCTURAL half of `validate_issue_body` (sections, budget
  line) — an open `Blocked by #N` is real for `is_dispatchable` and `promotable_to_ready`, which
  still must never treat a blocked issue as dispatchable or promotable, but it is not a defect the
  refiner wrote or can rewrite away. Counting it as one would have relaunched the refiner on #356
  (template-conformant, `status:refine`, `Blocked by #323` still open) for nothing — found once
  `planner.refiner_unattended` went live and fixed the same day.
