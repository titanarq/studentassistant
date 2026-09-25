# A PR with no checks fails the CI condition, and every host ships a CI that reports on every PR

- Date: 2026-09-24
- Status: accepted (clarifies condition 1 of
  `2026-09-17-the-control-plane-merges-a-pr-in-the-humans-name-under-five-conditions.md`)
- Issue: agent-os#50

## Context
Condition 1 of the control plane's merge gate — "CI green on the PR's HEAD SHA" — said nothing
about a head SHA on which no check is reported at all. That is the normal state of a freshly
installed host: `agent-os-install` copied only `ci-agent-os.yml`, which is path-filtered to
`agent_os/**`, so every PR touching only host files reported zero checks. A host whose own CI task
was blocked by its first skeleton PR deadlocked: the skeleton could not merge without CI, and the
CI task could not start before the skeleton merged. The validator had approved, conditions 2-5
held, and the host's test command was green on a clean worktree of the head; the control plane
handed the PR back to the human.

## Decision
- **Zero checks reported on the head SHA (no check run and no status) means condition 1 is not
  met.** The control plane does not merge; it hands the PR back to the human with that reason.
  Condition 1 stays strict.
- **The mechanism makes sure no PR lacks a check.** `agent-os-install` also renders
  `agent_os/templates/ci-host.yml` into `.github/workflows/ci-host.yml`: a workflow running
  `project.test_command` on every `pull_request`, with no path filter. It follows install's
  existing conventions — written only if absent, a differing file is reported and left alone
  unless `--force`. A host whose own CI already reports on every PR opts out with
  `project.install_host_ci: false` (default `true`).
- **`agent-os-doctor` fails when no workflow would report on a host-only PR**: no file under
  `.github/workflows/` whose `on:` block fires on `pull_request` without a `paths`/`paths-ignore`
  filter. The heuristic reads only the `on:` block and says so in its message.
- **A host lands its CI before its first product PR**, merged by hand if need be, and never makes
  its CI task depend on a skeleton (`docs/ADOPTION.md` step 23).

## Rejected alternative
"No checks" acceptable when no workflow's path filter matches the diff and the control plane ran
the host's test command green on the head SHA in a clean worktree, recorded in its review.
Rejected: the control plane merges in the human's name, and this would let it merge on evidence
only it produced, on its own machine, with nothing on GitHub any reviewer can re-read — a merge in
the human's name without real CI. The 2026-09-17 ADR already says no condition is waived; a
condition that cannot be met is fixed by making it meetable, not by an exemption.

## Consequences
- A host pulling this change runs `agent-os-install --dry-run` and then `agent-os-install` (or
  `--force` if it wants the rendered file over one it already has) to get `ci-host.yml`, or sets
  `project.install_host_ci: false` when its own CI already covers every PR. A PR already waiting
  with zero checks is merged by hand once, by the human.
- The rendered workflow is a starting point: a test command that needs a toolchain, services or
  dependencies beyond Python 3.12 needs those setup steps added by the host. Editing it makes a
  later `agent-os-install` report the difference and exit non-zero unless `--force`, the same as
  every other installed file.
