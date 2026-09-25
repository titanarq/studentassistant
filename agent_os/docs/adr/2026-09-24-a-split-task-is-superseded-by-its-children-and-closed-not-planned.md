# A split task is superseded by its children and closed as not planned

- Date: 2026-09-24
- Status: accepted
- Modules: refiner, tracker CLI
- Amends `2026-09-15-the-refiner-runs-unattended-only-after-a-human-reviewed-its-dry-run.md`,
  whose split left the original open with only its refine label removed

## Context
When the refiner split a task or bug into children, the original stayed open, untouched but for
its refine label. Every other open issue whose `## Dependencies` said `Blocked by #<original>`
then waited on an issue nothing would ever work or close: `validate_issue_body` kept reporting
`blocked by #N, which is still open` and the whole downstream chain never became dispatchable. A
human closing the original by hand was no better: the dependents unblocked at once, before any
child was done (#39). A host was rewriting the dependents by hand after every split.

## Decision
- **The children of a split task or bug replace it.** Every open `Blocked by #<original>` line is
  rewritten to `Blocked by #<child>` lines, one per child — all of them, unless the refiner can
  tell which ones a given dependent waits on and says so with `--route D=child[,child]`. Waiting on
  too many children is the safe error; waiting on too few unblocks early.
- **The original is closed as `not planned`**, with a `Superseded by #a, #b` comment, and each
  rewritten dependent gets a comment saying what changed and why.
- **It is one deterministic command, `issues.py supersede N --by <child>...`**, which the refiner
  prompt tells the refiner to run — not a sequence of edits the prompt describes. The rewrite reuses
  the same `BLOCKED_BY_RE` the dependency reader uses (`agent_lib.replace_blocker`), so the writer
  can never touch a line the reader would not take for a blocker.
- Dependents are rewritten **before** the original closes, so a run cut in between leaves every
  dependent still blocked, never unblocked. The command is idempotent: a second run finds no line
  to rewrite and an already closed original.
- **A feature is never superseded.** Its children are its parts, not its replacement; the command
  refuses a grouping type (the `epic`/`feature` labels of `project.labels.types`).

## Consequences
- A split no longer needs a human to keep the backlog moving.
- `validate` does not yet flag a blocker that was split but never superseded (a split done before
  this decision, or by hand); running `supersede` on it is the repair.
