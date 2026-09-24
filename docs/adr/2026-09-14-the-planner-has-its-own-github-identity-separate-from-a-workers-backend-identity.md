# The planner has its own GitHub identity, separate from a worker's backend identity

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
Workers already have per-backend GitHub identities (`roedor-claude`, `roedor-qwen`) precisely
because the backend is what defines their spend pool and capability — see
`docs/modules/workers.md`#Identities. The planner is a different kind of actor: it never edits
code, it decides what runs next, freezes or relaunches issues, changes labels, and comments
explaining why. Its comments and label changes need to read, at a glance, as a planning decision —
not as a worker's own commit trail, and not as the human's own voice, which is the one thing the
existing identity rule already protects (`the user's own account is the only person`). Which
backend happens to execute a given planner tick is an implementation detail the issue history
should not have to surface.

## Decision
A third GitHub App, `roedor-planner`, distinct from the two worker identities and from the human
account. Every label change, relaunch, freeze, or explanatory comment the planner makes is
attributed to it — never to `roedor-claude`/`roedor-qwen` (whichever backend happens to be running
the planner's reasoning that tick) and never to the user's own account. The pattern going forward is
one GitHub App per **role**, not per backend, with the worker role as the one deliberate exception
(there, backend and role coincide because the backend is what the identity needs to distinguish).
QA and PR-reviewer roles were floated in conversation but are not designed or decided — no app for
either yet.

## Consequences
- Reading an issue's history tells you, without opening a diff, whether a given event was a worker
  doing the work, the planner deciding what happens next, or the human — three distinct voices, not
  two.
- `scratchpad/restructure/github_apps_setup.md`'s setup steps and issue #337 (creating the GitHub
  Apps, a browser step) now cover three apps, not two.
- `docs/modules/workers.md`'s Identities section needs a third entry once the implementation lands.

## Source
Decided in conversation on 2026-09-14, issue #336.
