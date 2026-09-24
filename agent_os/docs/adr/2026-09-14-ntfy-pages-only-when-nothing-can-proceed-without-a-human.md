# Ntfy pages only when nothing can proceed without a human decision

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
Every routine cut in this design already has an automatic next step — the planner retries, falls
back to the other backend, or waits for a window to reset — so a notification on every one of them
teaches the habit of ignoring notifications, which defeats the one case that actually needs
immediate attention.

## Decision
One ntfy.sh topic for the whole project, a random string prefixed with the project name (e.g.
`roedor-x4$i3kv`) so the public topic is not guessable from the repo name; the topic string lives in
`.secrets/`, gitignored, never in `config/*.yaml` or in memory. Exactly two triggers page it:
- Claude's window is exhausted and a queued task specifically needs Claude reasoning (its
  `config/agents.yaml` class does not allow the Qwen backend), or Qwen is also out — see
  `agent_os/docs/adr/2026-09-14-quota-exhaustion-is-read-from-the-backend-not-claimed-by-the-agent.md`.
  Message names the wait and the action: "Claude sin cupo (resetea en Xh); ¿activo la cuenta de
  respaldo?"
- The planner, on a tick, finds every dispatchable issue either `status:blocked-on-human` or past
  its relaunch cap (see
  `agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md`) —
  nothing it can advance on its own. Message names which issue and why.

A worker being cut, a stall warning, a single relaunch, or a normal `DONE` never pages — those are
read from GitHub or `tracks.sh` when convenient.

## Amendment 2026-09-16 (issue #366): a third trigger, and no message written in code

There is a **third** trigger: an issue reaching `status:review`. It belongs here by the same test
as the other two — an approved PR is the one thing only the human can finish, and nothing else in
the mechanism moves it — and it was the touchpoint with the least warning of all
(`agent_os/docs/AGENT_OS.md` §2.1, row 5: an approved PR could sit unmerged indefinitely with no push
signal at all). `issues.py move N review` sends it, after the label and the board column are
written, and **once per issue**: a re-validation or a reopened PR moving through `review` again
pages nobody, which is the same "once" the daily planner cap already used, recorded the same way,
in a marker file under `.cache/`. A page that fails never fails the move — the state it announces
is already written.

And no page is composed in code any more. Every message is a one-line template under
`project.messages` in `config/agents.yaml`, written in `project.human_language`, rendered by
`agent_lib.render_human_message`, which refuses a key the project never wrote and a placeholder
nobody passed a value for. A template `str.format` cannot parse — a positional `{}`, a stray `{`
inside a Spanish sentence — is refused when the **config loads**, on every command, instead of at
the one moment something needed to page; and every failure it can still have is one exception
type, `HumanMessageError`, because every caller does the same thing with it. **No page ever fails
the state change it announces**, anywhere: the cut is already committed, the cap has already
stopped the day, the label and the board column are already written, so a page that cannot be
rendered or sent is printed to the journal and the run carries on — a misspelled key must not
abort a tick, and with it the other backend's check, over a typo nothing at that point can act on. The quota-exhaustion page above was a Spanish literal inside
`agent_guard.py`, so a project adopting this mechanism with `human_language: English` still got
that one page in Spanish (`agent_os/docs/AGENT_OS.md` §7 row (g)) — the rule that a question for the human
is written in their language (`agent_os/docs/adr/2026-09-15-a-question-for-the-human-is-written-in-their-
language-and-in-functional-terms.md`) held everywhere except on the one channel that reaches their
phone. There are five keys, one per mechanical page, and there are no others: `review_ready`,
`quota_exhausted_no_fallback`, `planner_run_cap_reached`, `backend_worktree_missing`
(#392's amendment below) and `unreviewed_pull_request` (#394's amendment below) — the last two are
each the second trigger above in its concrete form, the same way `planner_run_cap_reached` already
was, not a trigger of their own. The operational line a run prints to its journal stays English and
separate from the page, because they have different readers.

## Amendment, 2026-09-16 (#392)
A fourth mechanical page exists — the guard, once per `planner.idle_wake_minutes` per backend, when
a backend's configured worktree is missing and ready work resolves to it — and it is **the second
trigger above in its concrete form, not a new rule**: on that backend nothing can proceed, the
driver would refuse every dispatch with `no worktree at <path>`, and only a human can create the
worktree (`gh pr merge --delete-branch` deletes it, `agent_os/docs/AGENT_OS.md` §7 row (r)). It is written
as a page and not a planner event for exactly the reason this ADR gives: there is nothing for
the planner to advance. It does not wait for the whole dispatchable set to be empty — another
backend having work of its own does not make this one any less stopped — but it stays rate-limited,
so the habit of ignoring notifications is not what it teaches. Its wording is the fourth
`project.messages` key, `backend_worktree_missing`, rendered the same way the amendment above
requires, and a template it cannot render is printed and skipped rather than allowed to stop the
tick.

## Amendment, 2026-09-17 (#394)
A fifth mechanical page exists — the guard, once per pull request per `planner.idle_wake_minutes`,
when a `status:ai-completed` issue's pull request carries no review and no one-shot role is alive on
it. Unlike the other four, the planner is not actually stuck on it: `scripts/planner_task.sh`
already relaunches a validator on every `status:ai-completed` issue with no review each time it
runs at all, so this is not a permanent block the way a missing worktree is. What earns it a page
anyway is the evidence this ADR did not anticipate when it was written: a one-shot role can die
leaving NOTHING on disk at all — no PID file, no `runs.tsv` row, no event (PR #391, 2026-09-16) — so
the `role_died` mechanism (#400) that would otherwise bring the planner back to it has nothing to
find, and the review the mechanism promised can silently not happen. A human seeing it once is what
breaks a loop that would otherwise repeat unannounced every time a run dies the same way, with
nothing on disk for the planner's own bookkeeping to ever notice. The condition and its page are
`agent_guard.unreviewed_completions` and `agent_guard._page_unreviewed_completions_if_due`. Its
wording is the fifth `project.messages` key, `unreviewed_pull_request`, rendered the same way the
amendments above require, and a template it cannot render is printed and skipped rather than
allowed to stop the tick.

## Consequences
- A page always means "only you can move this forward now."
- The topic being unguessable is the only protection ntfy.sh's public server gives; it carries no
  content by itself and reveals nothing if guessed, but is still handled like any other bearer
  credential.

## Source
Decided in conversation on 2026-09-14, issue #336.
