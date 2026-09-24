# The control plane merges a PR in the human's name, only under five conditions

- Date: 2026-09-17
- Status: accepted (amends `2026-08-26-the-agent-proposes-the-human-publishes.md` as the agent
  mechanism reads it — "the human merges" — and the consequence "Merge stays a human act" of
  `2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-acceptance-criteria.md`)
- Modules: workers

## Context
`2026-08-26-the-agent-proposes-the-human-publishes.md` decided, for calibration, that an agent
proposes and a human publishes. The agent mechanism took that rule as the ground for its last
step: `2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-acceptance-criteria.md`
cites it for "the human merges" and records "Merge stays a human act; letting the planner merge an
approved PR is a separate decision", and `agent_os/docs/AGENT_OS.md` §1 lists `status:review` → `done` as
"Human, exclusively".

Since 2026-09-15 (PR #373) an agent exists that is none of the mechanism's four roles: the
**control plane**, `.claude/agents/control-plane.md`, the human's delegate over the mechanism. It
authenticates every `gh` call as the human (`project.human_login`), and one of its five duties is
to approve and merge PRs. It merged PRs #399 and #401 on 2026-09-17, for instance.
The written record therefore said "only the human merges" while the human's own delegate merged;
`agent_os/docs/AGENT_OS.md` §2.4 and the *Identities* section of `docs/modules/workers.md` document the role
(#371, PR #403), and this ADR brings the decision in line with them.

## Decision
- **A PR produced by the agent mechanism may be merged by the human or by the control plane
  acting in the human's name.** The control plane is not a mechanism role: no planner, guard,
  driver or event starts it, and it signs as the human, never as a GitHub App.
- **It merges only when all five conditions hold, each verified by itself against GitHub and the
  diff, never taken from the PR text.** The binding text is "Duty 4" of
  `.claude/agents/control-plane.md`, restated in `agent_os/docs/AGENT_OS.md` §2.4:
  1. CI is green on the PR's HEAD SHA and the PR targets the default branch.
  2. The validator approved it — or no validator review exists and the control plane reviewed the
     diff against the issue's acceptance criteria line by line.
  3. The diff touches only files the issue's scope allows, none of `project.forbidden_paths`, and
     nothing an `AGENTS.md` rule freezes.
  4. No test was removed or weakened, compared by content after `ruff format` on both sides.
  5. The PR body closes exactly the issue it was dispatched for, and the module doc changed if
     behaviour or a contract changed.
  If one fails it requests changes with the evidence, leaves `status:review`, and does not merge
  to unblock a round.
- **No condition is waived, not even on the human's say-so.** On 2026-09-17 the human accepted
  two PRs with one condition unmet, to unblock the round: #401 without a validator approval (the
  failing test was already broken on `main`, now #404) and #403 with `scratchpad/progress.log`
  lines outside its scope. Those were one-offs, not a precedent: if a condition turns out to be
  wrong, the fix is a bug against the condition, never a merge that skips it.
- **The mechanism's own roles still never merge.** The planner, the validator, the refiner and the
  workers propose, review and label; the "separate decision" the 2026-09-14 ADR leaves open for
  the planner is not taken here.
- **What stays the human's own hand is unchanged:** any question the written record does not
  settle, `status:agents-paused` on the tracking epic, and `auto-ready` on a feature. The
  control plane never sets or removes either label.
- **Calibration is untouched.** The 2026-08-26 decision that the human publishes a configuration
  version stands as written; this amendment covers only merging PRs that come out of the agent
  mechanism.

## Consequences
- The audit trail cannot tell a control-plane merge from the human's own: both are
  `project.human_login`. The written record — the issue comment the control plane leaves on every
  merge, its report — is the only thing that separates them. Giving it a GitHub App of its own is
  a later decision, not taken here.
- `agent_os/docs/AGENT_OS.md` §1's row `status:review` → `done` ("Human, exclusively") no longer holds as
  written and must name the control plane too; the planner's rules in `scripts/planner_task.sh`
  ("the human merges -- you never merge, and neither does it") stay true for the planner and the
  validator.
- A merge done in the human's name is exactly as binding as one done by the human; a condition the
  control plane cannot verify is a reason not to merge, not a reason to ask the mechanism to.

## Source
Decided by the human on 2026-09-15 with the control-plane agent (PR #373, "the human's delegate
over the mechanism"); written down on 2026-09-17 at the human's request, issue #371 (its *Not
included* names this amendment), under #336.
