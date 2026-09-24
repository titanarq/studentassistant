# The mechanism is one directory, extended by hosts and never modified

- Date: 2026-09-21
- Status: accepted (makes `2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md`
  true to the letter, and amends condition 3 of
  `2026-09-17-the-control-plane-merges-a-pr-in-the-humans-name-under-five-conditions.md` — see
  the last section)
- Modules: workers

## Context
The 2026-09-14 ADR decided that the mechanism is project-agnostic: configured, not coded. It also
decided *how* that would be kept true — "the mechanism's scripts and their tests are **grouped** so
they can be lifted as one unit" — and grouping is what failed. The files stayed in roedor's own
`scripts/` and `tests/`, beside roedor's own, and the coupling the audit of 2026-09-21 found was not
in the values (those had moved into `config/agents.yaml`) but in the plumbing:

- `.venv/bin/python` as a literal in ~45 call sites: the mechanism ran on the HOST's interpreter,
  so it worked only in a host that happens to have a virtualenv at its root carrying `pyyaml` and
  `pydantic`. The second host, `../twistedworldar`, has no root virtualenv at all.
- `from scripts.agent_lib import …`: an import that resolves only through roedor's own editable
  install. In any other host it is an `ImportError`, and inside roedor it resolves to the MAIN
  checkout's copy whatever tree the caller is in.
- Every path derived from `__file__`'s grandparent, which was the host's root only because the
  mechanism's files sat in the host's `scripts/`.
- The mechanism's tests in `tests/`, sharing a `conftest.py` whose module scope imports the host's
  database layer, under a `pyproject.toml` whose `testpaths` is the host's.

A group of files is not a unit. A directory with its own package metadata, its own interpreter and
its own tests is.

## Decision
- **The mechanism is one directory, `agent_os/`, and it is an installable Python package.** Its
  code is `agent_os/agent_os/{lib,guard,issues,gh_app_token,cli}.py`, its drivers are
  `agent_os/bin/*.sh`, its tests are `agent_os/tests/` with their own `conftest.py`, and its
  metadata is `agent_os/pyproject.toml`. Internal calls go through the package
  (`python -m agent_os.lib …`), never through a path into a host's `scripts/`.
- **It runs on its own interpreter.** One resolver, written once in each language —
  `agent_os_python()` in `agent_os/bin/_python.sh` and `agent_os.cli.agent_os_python()` — answers:
  `$AGENT_OS_PYTHON` if set, else the virtualenv `agent_os/bootstrap.sh` builds beside the package,
  else `python3`. No call site names an interpreter. A host's own interpreter is reached through
  exactly one configured door, `project.test_command`, which is how a worker runs the host's tests.
- **The host's root is resolved, not assumed.** `$AGENT_OS_HOST_ROOT`, else the git checkout the
  call is made from, else the directory above the package — never the package's own location.
  Everything a project owns hangs off that path: `config/agents.yaml`, `.cache/`, `.secrets/`,
  `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries.
- **A host EXTENDS the mechanism and never MODIFIES it.** The three extension points are
  `config/agents.yaml`, host-owned files that config names, and hook commands config names. A
  change made inside `agent_os/` for one project is a defect: it would be lost on the next pull,
  and it would carry that project's assumption into every other one.
- **It is consumed by `git subtree`.** `git subtree split --prefix=agent_os` produces the
  standalone repository; each host pulls it back with `git subtree pull --prefix=agent_os`. An
  improvement made while running one project reaches the others by pulling one directory.
- **In roedor, the `scripts/` entry points remain as shims: that is the compatibility contract.**
  `scripts/{agent_guard,agent_lib,issues,gh_app_token}.py` and
  `scripts/{worker_task,agent_task,planner_task,worker_progress,notify,qwen_task}.sh` are each a
  short `exec` into `agent_os/`. They exist because a systemd unit, a `.claude/agents/*.md` prompt,
  `CLAUDE.md`, a module doc and a memory note all name those paths, and because the worker
  worktrees execute the MAIN checkout's drivers — so a rename with no shim breaks a run in flight.
  A shim adds no behaviour and takes no argument of its own.

## Consequences
- `main` keeps the live mechanism working at every commit, which is why a rename only ever lands
  together with its shim, in the same commit.
- The mechanism's own dependency set is declared where it is used (`pyyaml`, `pydantic`, `PyJWT`,
  `requests`) instead of being inherited from whatever the host installed.
- CI runs two suites: the host's `pytest -m "not db"` and the mechanism's own
  `agent_os/.venv/bin/pytest agent_os/tests -q`. The second is the one a second host can also run.
- A test of the mechanism that asserts a roedor value, or imports `roedor`, is now visible as what
  it is — a host coupling inside a portable directory — instead of being invisible because
  everything around it was roedor too. `agent_os/tests/test_agent_lib.py` still imports `roedor`
  for its `m2`-fingerprint test; moving it out is a task of its own.
- `mechanism.own_paths` names `agent_os/*` and the shims, so the mechanism can still develop
  itself under the rule of
  `2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md`.
- The interpreter resolver's last step is a fallback and not a promise: a `python3` with none of the
  declared dependencies fails on the import. That is the correct answer to "the mechanism was never
  bootstrapped", and it is why `bootstrap.sh` is one command.

## Amendment: condition 3 of the merge gate audits a subset
`2026-09-17-the-control-plane-merges-a-pr-in-the-humans-name-under-five-conditions.md` states
condition 3 as "the diff touches only files the issue's scope allows, none of
`project.forbidden_paths`, and nothing an `AGENTS.md` rule freezes". Issue #476 showed that
`project.forbidden_paths` does two different jobs with one list and that the second one is wrong
for two of its entries: the list was written as *what a worker may not write*, and condition 3
reused it as *what may not enter `main`*. For the stamp entries the two readings coincide. For
`config/proposals/*` and `docs/adr/*` they do not — those are **delivery directories**, and neither
enters the metrics fingerprint, so a diff that adds a file under one changes nothing the freeze
protects. As written, the condition refused every PR carrying the ADR that `AGENTS.md`'s *Definition
of done* requires: the rule that defines a finished job and the gate that admits it contradicted
each other, and three PRs (#466, #469, #472) were merged by hand on 2026-09-20 in that state.

**Condition 3 therefore reads, from 2026-09-21:** the diff touches only files the issue's scope
allows, none of the **merge-audited subset** of the host's protected paths — `project.forbidden_paths`
minus `project.merge_audit_exempt_paths`, which is what
`agent_lib.forbidden_paths_merge_audit_regex` renders and what the
`forbidden-paths-merge-audit-violations` subcommand prints for a list of changed paths — and nothing
an `AGENTS.md` rule freezes.

What does not change: the worker's own ownership audit and its "FILES YOU MUST NOT TOUCH" paragraph
keep reading the **full** `project.forbidden_paths`, unconditionally, because a brief cannot
authorize a worker to write one; the exemption is a statement about *merging a reviewed diff*, not
about what an agent may write on its own. Nothing was added to or removed from
`project.forbidden_paths` itself. And the principle the 2026-09-17 ADR states about its own
conditions is what produced this amendment rather than a waiver: "if a condition turns out to be
wrong, the fix is a bug against the condition, never a merge that skips it".

## Source
Decided by the human on 2026-09-21, epic #507 ("the agent OS is one directory that leaves as a
unit"), task #508. The audit that listed the couplings is recorded in #507's *Context*. The
condition-3 amendment is the last acceptance criterion of #476, whose own diff was forbidden from
carrying a file under `docs/adr/`; it is written here rather than in a file of its own because the
same directory move is what finally gives the mechanism's decisions a home.
