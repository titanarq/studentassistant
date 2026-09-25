# A fresh worktree is provisioned the way the host configures

- Date: 2026-09-24
- Status: accepted
- Issue: agent-os#41
- Modules: workers

## Context
A new git worktree carries tracked files only. Everything a project's commands need beside them —
a virtualenv, `node_modules`, a `.env` — is gitignored, so the drivers made a fresh worktree
runnable themselves: `agent_prepare_worktree` (the validator's throwaway worktree) and
`worker_task.sh <backend> init` both symlinked `<host>/.venv` and `<host>/.env` into it, and
nothing else. The validator's prompt then told the agent the worktree was "already populated with
the environment its commands need", forbade it from preparing one, and hard-coded
`.venv/bin/ruff`.

That holds for exactly one layout: a host whose whole environment is a virtualenv at its root that
already exists in the main checkout. A monorepo keeping its Python environment in `backend/.venv`
(a uv project in a subdirectory) and its web one in `web/node_modules` got an empty worktree; its
validator could run nothing, and requested changes on correct code. The same prompt also carried
two sentences true of the first host only: that `pytest` writes to a shared database (so a
validator on another host waited for unrelated pytest processes on the machine) and that the full
suite takes ~50 minutes.

## Decision
1. **Provisioning is configuration.** Two `project:` keys, read by one shell helper,
   `agent_provision_worktree` in `bin/agent_task.sh`, which both the validator's worktree and
   `worker_task.sh init` call:
   - `worktree_links` — paths relative to the host root, each symlinked from the main checkout into
     the worktree when the checkout has it and the worktree does not. A link, never a copy. Default
     `[.venv, .env]`: exactly what both drivers did before, so a host that sets nothing is unchanged.
   - `worktree_setup_command` — run by `bash -c` inside the worktree after the links and before any
     backend starts. Default empty: nothing runs.
2. **A setup that fails refuses the run.** A validator launched on a tree its own host could not
   provision reports the environment's failure as the code's, which is the defect itself. So a
   non-zero setup stops the driver before the backend starts (the throwaway worktree is removed by
   the same EXIT trap as ever), and `init` removes the half-made worktree and its branch so the
   next `init` starts from nothing rather than calling it initialized. A worktree that cannot be
   *created* at all keeps its old behaviour — a WARNING, and a validator that reads the diff only —
   because that is a different failure with a run that can still say something true.
3. **The prompt says only what the mechanism knows.** The validator is told the driver provisioned
   the worktree "the way this project configures", and that a command failing there for something
   missing is a finding about the environment, never a verdict on the code. Its lint bullet renders
   from `project.lint_commands` (each command gets the touched files appended) and disappears when
   the list is empty. The shared-database sentence and the suite duration are removed rather than
   configured: the first is a `never_run` reason or a validator `prompt_extras` paragraph for the
   host that needs it; the second only ever served to say "the full suite is expensive", which the
   prompt now says without a number.

## Consequences
- A monorepo sets `worktree_setup_command` to its own bootstrap and, if it wants, empties
  `worktree_links` of the root `.venv` it does not have (a missing source is skipped anyway).
- The validator's template carries no host literal any more; `LITERALS_A_TEMPLATE_STILL_CARRIES`
  in `tests/test_agent_task.py` is empty and §7 row (aa) of `docs/AGENT_OS.md` is closed.
- A host that relied on the hard-coded ruff bullet sets `lint_commands`; `config.example.yaml`
  shows the two ruff commands the bullet used to spell.
- `worker_task.sh`'s per-launch `.env` link (`launch_stage`) is untouched: it repairs a worktree
  that lost its `.env` between runs, and its #404 bookkeeping is specific to that one file.
