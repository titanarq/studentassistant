# Adopting the mechanism on a second host

A numbered checklist, in the order a second host actually does it, for taking `agent_os/` as one
directory (§4.1) and running it against a new project. Nothing here is edited inside `agent_os/`:
every step either writes a file outside it or fills in `config/agents.yaml`
(`agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-never-modified.md`).
Placeholders: `<host>` is the new project's checkout root, `<org>/<repo>` its GitHub repository,
`<remote>` the git remote the mechanism's own repository is added as
(`git remote add agent-os https://github.com/titanarq/agent-os.git`), `<agent>` a backend or role
identity (`qwen`, `claude`, `planner`, `validator`, `refiner`).

## 1. Knowledge-layer prerequisites

The mechanism reads a project's own knowledge layer at several points (the worker's brief, the
`__MODULE_DOCS__` token, the merge gate's protected-paths audit); a host with none of it, like
`../twistedworldar` today, writes it before anything below.

1. An `AGENTS.md` at `<host>/AGENTS.md` — how the project is organized and how a task starts. The
   mechanism does not read it directly, but every prompt template assumes an agent can.
2. One module doc per name you intend to put in `project.modules` (§4.2), under
   `<host>/docs/modules/<name>.md` by default — the path is `project.module_docs_dir`, which
   renders the `__MODULE_DOCS__` token in `.claude/agents/*.md`.
3. A `<host>/docs/adr/` directory — even empty, so `project.forbidden_paths` can name
   `docs/adr/*` (§4.2's own example) and the first ADR the mechanism itself prompts for has
   somewhere to land.
4. A single test command — one wrapper script, e.g. `<host>/scripts/test.sh`, that runs the whole
   suite compactly. Its path becomes `project.test_command` (§4.2), injected as
   `__TEST_COMMAND__` into the worker's prompt; a host with more than one suite (Python backend,
   Kotlin app, Node tooling — `../twistedworldar`'s actual shape) names the one wrapper that runs
   the right one, or a script that runs all of them.
   If the environment that command needs is not a virtualenv at the host's root — a monorepo
   with `backend/.venv` and `web/node_modules`, say — set `project.worktree_links` to the
   gitignored paths a fresh worktree should link from the main checkout, and/or
   `project.worktree_setup_command` to the bootstrap that builds them inside the worktree
   (`uv sync --frozen`, `npm ci`): the validator's throwaway worktree and each worker's `init` run
   it, and a failing one refuses the run (agent-os#41). Set `project.lint_commands` to the
   linters the validator should run on a PR's files; left empty, it runs none.
5. A default branch named `main`. This is not a `project.*` key: `git worktree add`/`branch`/
   `open-pr` in `agent_os/bin/worker_task.sh` fetch and fork from the literal `origin/main`
   throughout (§4.1's own list of files, the `init` and `branch` cases). A repository whose default
   branch is `master` or anything else must rename it on GitHub before adopting the mechanism, not
   after.
6. A `.gitignore` covering `.cache/` and `.secrets/` (or whatever `project.secrets_dir` names) —
   the mechanism writes worker state, event logs and App private keys into both and assumes
   neither is ever tracked.

## 2. Bring the mechanism in and configure it

7. **Copy `agent_os/` as a unit** into `<host>/agent_os/` —
   `git subtree add --prefix=agent_os <remote> main --squash` from `titanarq/agent-os`
   (§4.1's own ADR), or a plain `cp -r` for a first look. Nothing under it is edited;
   a host extends it only through `config/agents.yaml`, host-owned files that config names, and
   hook commands. From the host's root, over HTTPS:
   ```bash
   git remote add agent-os https://github.com/titanarq/agent-os.git
   git subtree add --prefix=agent_os agent-os main --squash
   ```
   `titanarq/agent-os` is public, so this fetch — and every later `subtree pull` (step 25) —
   needs no credential. Two cases do: `subtree push` (step 26) always, and every fetch when the
   remote is a private fork or mirror. Then **git itself** has to present a GitHub credential for
   an account that can write the repository (for a push) or read it (for a private fetch), and a
   `gh` login is not that by itself. A token in `GH_TOKEN`, a non-interactive
   `gh auth login --with-token`, or a "no" to the interactive login's "authenticate Git" question
   all leave git with no credential helper for `github.com`, and the `https://` push or fetch
   stops on an auth prompt. Point git at `gh`'s login once per machine, before the first such
   command:
   ```bash
   gh auth setup-git       # registers gh as git's credential helper for every host gh is logged in to
   ```
   The `repo` scope step 17 already asks for covers both reading a private repository and
   pushing. An SSH remote (`git@github.com:<owner>/<repo>.git`) with a key registered on such an
   account works instead and needs no credential helper.
8. **Write `<host>/config/agents.yaml`** from `agent_os/config.example.yaml`, which carries every
   key of §4.2 filled in for an invented project: copy the `project:`/`mechanism:`/`planner:`
   structure and fill in each key against what step 1–6 just wrote (`project.modules` from item 2,
   `project.test_command` from item 4, `project.module_docs_dir` if item 2 used a different path);
   `classes:` can be copied as a starting point and tuned later (§3).
9. **Set `project.forbidden_paths`, `project.merge_audit_exempt_paths` and
   `project.worker_environment`** — the host's own protected-path globs and the KEYS (never the
   values) of whatever a worker's backend process needs exported. Leaving either list empty
   renders no such paragraph and no such audit rule. A fourth, optional key here:
   `project.prompt_extras`, one host-owned file per role appended at that role's
   `__PROJECT_EXTRAS__` point — a host that names none renders nothing there.
10. **Set `project.never_run`** — commands no role may run, each with the one-line reason its
    prohibition rests on; renders into the worker's, the validator's and the refiner's RULES.
11. **Set `mechanism.own_paths`** — copy the example's `agent_os/*` entry unchanged and add any
    host-side shim scripts the host keeps outside `agent_os/`; this is the mechanism's own files
    list, not the host's `forbidden_paths` (§4.1, §7 row (u)).

## 3. Things to create in GitHub (§4.3)

12. **Labels** — `type:epic`, `type:feature`, `type:task`, `type:bug`; `p1`..`p4`; one
    `module:<name>` per `project.modules` entry; the six state labels (`status:refine` …
    `status:ai-completed`, `status:review`) self-create on the first `agent_os.issues move` into
    each state, but `status:agents-paused`, `auto-ready` and `wake:planner` do not and must be
    created by hand on `<org>/<repo>` before the first real run — they are the three
    `agent-os-doctor` checks for (step 21), under the names
    `project.labels.{agents_paused,auto_ready,wake_planner}` give them.
13. **A Project (v2) board** on `<org>/<repo>` with a single-select field named exactly `Status`
    (any other name falls back silently to the first single-select field found) and six options
    matching `project.board_columns`: `Backlog`, `Ready for AI`, `In progress`, `AI completed`,
    `Review`, `Done`.
14. **One GitHub App per identity** — `project.backends.<name>.app` for each worker backend
    (`project.backends.qwen.app`, `project.backends.claude.app` in the example),
    `project.planner_app`, and optionally `project.role_apps.validator`/`.refiner` (falling back to
    `planner_app` when unset, §7 row (p)). This is a browser step with no manifest automation in
    the mechanism (`agent_os.gh_app_token` only mints tokens for an App that already exists):
    create each App on `<org>/<repo>`'s GitHub settings, with permissions matched to what that
    role calls — workers need Issues (read/write), Contents (push), Pull requests (create); the
    planner needs Issues, Pull requests (read), Projects; the validator additionally needs "Pull
    request reviews" — install it on the repo, and download its private key. A worker App
    without the Workflows permission cannot push a **stale** branch: GitHub refuses to create a
    ref whose tree differs from the default branch under `.github/workflows/`, even when none of
    the branch's own commits touch a workflow — which is exactly the branch `open-pr` pushes
    unmerged after a conflict with its base (agent-os#61). `open-pr` then writes
    `BLOCKED reason=workflows_permission`, comments the refusal on the issue and moves it to
    `status:blocked-on-human`; resolving the conflict (merge `origin/<base>` into the branch,
    push, `open-pr` again) unblocks it. Granting the worker App Workflows (read/write) avoids it.
15. **`<slug>.json` + `<slug>.pem` per identity**, under `project.secrets_dir` (`.secrets/gh_apps`
    by default): the `.pem` is the App's downloaded private key; the `.json` is
    `{"app_id": <id>, "installation_id": <id-or-omitted>, "private_key_path": ".secrets/gh_apps/<slug>.pem"}`
    (`agent_os.gh_app_token`'s own docstring — `installation_id` is discovered and written back on
    first use when omitted).

## 4. Things to create on the machine (§4.4)

16. **The mechanism's own interpreter** — `bash agent_os/bootstrap.sh` builds `agent_os/.venv` and
    installs the package into it, editable, with `pytest` and `ruff`. Idempotent; run it again
    after every `git subtree pull` (step 25).
17. **Binaries** — `gh` (authenticated with `repo`+`project` scopes), `git`, `python3.12`, the
    backend CLI(s) a role runs (`claude`, `qwen`, or whichever the host configures), `curl`
    (`agent_os/bin/notify.sh`), `ruff==0.16.4` (CI), `systemd --user`. Point
    `project.executables` at any of these whose PATH the launching shell (a systemd user unit,
    typically) does not carry.
18. **One worktree per backend** — `agent_os/bin/worker_task.sh <backend> init`, once per
    `project.backends` entry that sets a `worktree`: idempotent `git worktree add` on a fresh
    branch from `origin/main` when the configured path has no `.git` yet, plus a `.venv`/`.env` symlink from the host root when
    either is missing.
19. **`.secrets/`** — `<secrets_dir>/ntfy_topic` (the ntfy.sh topic string) and the App
    `.json`/`.pem` pairs from step 15, if not already placed there; `<host>/.env` at the repo root
    for any credential a worker's backend process needs — both are written by hand, nothing in the
    mechanism creates a credential.
20. **`agent-os-install [--dry-run] [--force]`** — renders the systemd `--user` units
    (`~/.config/systemd/user/<guard_unit>.{service,timer}` and `.service.d/override.conf`) from
    `agent_os/templates/systemd/*.tmpl` and `project.guard_unit`/`project.executables`, with
    `ExecStart=` on the interpreter from step 16 (never a `.venv` at the host root; install refuses
    when step 16 has not been run and `AGENT_OS_PYTHON` is unset); copies
    `.claude/agents/{control-plane,worker-runner}.md` (rendered from `agent_os/agents/*.md`),
    `.github/ISSUE_TEMPLATE/{task,bug}.md` and `.github/workflows/ci-agent-os.yml`, each only if
    absent — and `.github/workflows/ci-host.yml`, rendered with `project.test_command`, which runs
    on every pull request with no path filter. Keep it unless your own CI already reports a check
    on every PR (then set `project.install_host_ci: false`): `ci-agent-os.yml` only fires on
    `agent_os/**` (and on a change to itself), and the control plane never merges a PR whose head
    SHA reports zero checks. For the same reason, never make `ci-agent-os.yml`'s job a required
    status check in branch protection: on a host-only PR it does not run, so it never reports. The
    rendered file is a starting point — add the setup your test command needs before its step.
    Never overwrites without `--force`, and never arms, restarts or reloads a unit — `--dry-run`
    first shows every path it would touch and its diff against what is there.
21. **`agent-os-doctor`** — reads the whole checklist above back in one pass: `gh auth status`
    scopes, the labels that do not autocreate, the Project v2 `Status` field and its six options,
    each App's secrets, each `project.executables` entry, each worktree, the notify topic file, the
    guard timer's `is-active`, and a workflow that reports a check on a host-only PR — one line per
    check, exit 1 on any failure. It never calls `agent_guard.py check` or any other trigger a
    role reacts to, so running it costs nothing.

(`agent-os-guard`, `agent-os-issues`, `agent-os-lib`, `agent-os-install`, `agent-os-doctor` are the
five console scripts `agent_os/pyproject.toml`'s `[project.scripts]` installs into
`agent_os/.venv/bin/`; the drivers themselves — `worker_task.sh`, `agent_task.sh`,
`planner_task.sh`, `notify.sh`, `worker_progress.sh` — are shell scripts under `agent_os/bin/`,
with no console-script equivalent.)

## 5. The first real run (§6)

22. Arm the guard timer — the one step nothing above does for you:
    `systemctl --user enable --now <guard_unit>.timer`.
23. Before moving any issue to `status:ready` for the first time: create the three labels that do
    not autocreate (step 12); make sure every issue meant for the trial is actually a Project item
    with a `Status` value set (an item can exist with no Status, or not be on the board at all);
    make sure each worker's worktree is on a fresh branch, not one left over from testing; decide
    by hand what to do with any issue stuck in `status:refine` whose parent carries no
    `auto-ready` — there is no mechanical way out of that state. **Land the host's CI first**
    (step 20's `ci-host.yml`, adapted, or your own), merged by hand, before the first product PR:
    a PR whose head SHA reports no check never meets merge condition 1, so a backlog whose CI task
    is blocked by a skeleton PR deadlocks — never make the CI task depend on a skeleton.
24. Watch the first run closely rather than trusting the configuration alone:
    `agent_os/bin/worker_task.sh <backend> watch` (tail the event stream),
    `journalctl --user -u <guard_unit>.service -f` (tick output), and
    `.cache/<role>/runs.tsv` (cost as it accrues).

## 6. Pulling improvements, and sending one back

25. **Pull** whatever the mechanism gained elsewhere since the last sync:
    `git subtree pull --prefix=agent_os <remote> main --squash`, then re-run step 16
    (`bash agent_os/bootstrap.sh`) so the new code and the interpreter agree, then step 21
    (`agent-os-doctor`) to confirm nothing the pull touched needs a new config key this host has
    not filled in yet.
26. **Send one back** — a fix or a generic improvement made while running this host belongs in the
    shared mechanism, not stranded here: commit it under `agent_os/` on a branch, then
    `git subtree push --prefix=agent_os <remote> <branch>` and open a pull request against the
    split repository's own `main`. A host-specific decision stays in `config/agents.yaml` or a
    host-owned file it names (step 9) instead — nothing that only makes sense for one project goes
    back through this door.
