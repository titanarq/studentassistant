# agent-os — agent instructions

## What this is
The agent operating system: a guard, a planner, workers and one-shot role drivers (validator,
refiner) that run a host project's GitHub backlog through headless coding agents, plus the tracker
CLI they share. It is **project-agnostic and configured, not coded**: a host extends it through its
own `config/agents.yaml` and never by editing a file in here
(`docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md`,
`docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-never-modified.md`).
The full picture — actors, identities, state layer, triggers — is `docs/AGENT_OS.md`.

Hosts consume this repository as a `git subtree --squash` under their own `agent_os/`. A defect is
fixed **here**, as a PR on this repository, and each host brings it in with
`git subtree pull --prefix=agent_os <remote> main --squash` on a branch of its own, merged with a
merge commit — never a squash, which drops the `git-subtree-dir:` metadata the next pull needs.

## Languages
Conversation with the user: Spanish. Code, identifiers, commits, repository documentation and code
comments: English. Code must be self-explanatory — long descriptive names, comments only for a
non-obvious "why".

## How a task starts
1. Read this file.
2. Read the issue (`gh issue view N`) and whatever it links: another issue, a host's issue, a PR.
3. Read only the sections of `docs/AGENT_OS.md` and the `docs/adr/*.md` the issue touches — then
   the code. `docs/ADOPTION.md` is the second-host checklist; issues about adoption land there.
4. The issue is a report, not a spec: reproduce the defect with a failing test first, then fix it.
   If the report's premise does not hold against the code, say so on the issue instead of building
   to it.

## Layout
- `agent_os/` — the Python package: `guard`, `lib`, `issues`, `install`, `doctor`, `render`, `cli`…
- `bin/` — the shell drivers (`worker_task.sh`, `agent_task.sh`, `planner_task.sh`, …) a host's
  `scripts/` wrappers `exec` into.
- `prompts/` — role prompt templates, rendered with host tokens (`__TEST_COMMAND__`, …).
- `agents/` — agent definitions a host installs into its own `.claude/agents/`; they are product,
  not tools for developing this repository.
- `templates/` — what `agent-os-install` writes into a host (issue templates, systemd, CI).
- `config.example.yaml` — the documented shape of a host's `config/agents.yaml`.
- `tests/` — the suite: no database, no network, no real backend.

## Commands
```bash
bash bootstrap.sh                                   # own interpreter in .venv (idempotent)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
TERM=dumb .venv/bin/pytest tests -q                 # the whole suite, as CI runs it
TERM=dumb .venv/bin/pytest tests/test_worker_task.py -q -k name   # one area while iterating
```
The whole suite takes over ten minutes: launch it in the background and check its exit code.
`TERM=dumb` matters — without it Rich wraps CLI error text in ANSI spans and message assertions
diverge from CI.

## Rules
- **No host literal anywhere outside `docs/`.** `tests/test_no_host_literals.py` reads every file
  git knows (`git ls-files --cached --others --exclude-standard`, except `docs/`, `tests/golden/`,
  `config.example.yaml`) and fails on a host project's name, org, owner login or database port.
  That includes untracked files that are not ignored: a stray file you leave in the tree is
  scanned; ignored paths (`.venv`, `.cache/`, `.claude/worktrees/`) and nested worktrees are not.
- A host-specific value (a path, a label, a runbook, a test command) is a config key with a
  documented default, never a literal in code, prompts or templates.
- **Tests must never launch a real backend.** A driver test without a fake `claude`/`qwen` first in
  `PATH` starts a real, billed run; every test that calls a driver stubs the backend binary.
- A golden fixture that changes is read diff by diff before it is committed: regenerating a golden
  certifies whatever the code prints, including a broken path.
- A path helper fails loudly when its premise (a `.git`, a config file, a package) is missing
  instead of falling back to `.` or a guess.
- Never kill by pattern (`pkill -f`, `pgrep -f | xargs kill`): stop a process by its PID.
- Never mutate the working tree while a pytest is running.

## Definition of done
A failing test that reproduced the defect now passes; ruff is clean; the relevant tests pass, and
the full suite if the change is wide; `docs/AGENT_OS.md`, `docs/ADOPTION.md` or `docs/CHANGELOG.md`
are updated when behaviour, a config key or the adoption steps changed; an ADR is written in
`docs/adr/` if a decision was taken; the issue carries the result. Work lands as a PR on a branch
(`fix/<N>-<slug>`), never straight on `main`, and the PR body says `Closes #N`.
