# Changelog

One entry per merged task of the parent epic (titanarq/roedor#507 — the mechanism separates from
its host, extended by config, never modified per project), in the order the work landed, newest
first. Every entry names the task issue and the pull request that closed it; entries for a task
that closed several small issues at once name them all. This file starts on 2026-09-21, the day
#508 moved the mechanism into `agent_os/` — nothing from before that date describes this directory.

## Unreleased

- agent-os#86 (PR #87) — a dirty worker worktree no longer deadlocks its backend unannounced.
  `worker_task.sh` writes `scratchpad/.gitignore` containing `*` in every worker worktree it
  touches (`hide_scratchpad_from_git`: before the dirty check in `start`, `resume` and `branch`,
  and before a cut's freeze), so an untracked diary or scratch file never refuses a dispatch in
  any run state and never lands in a `WIP: cut by guard` commit. The file lives in that worktree
  only and ignores itself; one the host tracks is left alone. A diary git tracks still shows as
  modified and #84 still covers it on `resume`. The #75 archive still empties `scratchpad/` for
  each new run under the same conditions, now listing ignored files too and keeping the
  `.gitignore`. What is still dirt is read by the guard on every tick (`backend_worktree_dirt`,
  same exclusions as the driver, nothing while a run is alive): the backend's ready issues leave
  the dispatchable set, so no planner run is woken to be refused, and the human is paged once per
  distinct listing with the new `project.messages.backend_worktree_dirty`; a clean worktree
  resets it. The issue asked for the page from the driver via `notify.sh`; the guard does it so
  the same read also ends the planner's retries (ADR
  `2026-09-25-scratch-is-invisible-to-git-and-a-dirty-idle-worktree-is-paged-by-the-guard.md`).
  `prompts/worker.md` and `prompts/planner.md` say so. Host follow-up: add
  `backend_worktree_dirty` to `project.messages` in `config/agents.yaml` (see
  `config.example.yaml`; without it the guard prints `no page for the dirty worktree`), and the
  `scratchpad/` entry in a worktree's `.git/info/exclude` can go.

- agent-os#84 (PR #85) — `worker_task.sh resume` over a run whose `.state` is `DONE` no longer
  refuses over that run's own diary. #22 left the diary out of `resume`'s dirty check only after
  `CUT_BY_GUARD`, on the premise that a finished run is never resumed; but a run that opened its
  pull request and exited is `DONE`, and it is exactly the one the planner resumes with the
  validator's request-changes review (`prompts/planner.md`, CHANGES REQUESTED). On a host with no
  ignore rule for `scratchpad/`, every request-changes loop was refused and needed a human.
  `drop_the_cut_runs_diary` is renamed `drop_the_resumed_runs_diary` and accepts `DONE` as well,
  same shapes, file untouched. Still refused: any other dirty path (another scratch file
  included), and a `.state` of `STARTED`, `RESUMED …`, `FAILED_LAUNCH …` or `BLOCKED …`. Host
  follow-up: the `scratchpad/` entry in a worktree's `.git/info/exclude` can go.

- agent-os#82 (PR #83) — `tests/test_no_host_literals.py` no longer requires `AGENT_OS_DIR` to
  hold its own `.git`. `_repository_files` demanded `root / ".git"` before ever asking git
  anything, but a host consuming this mechanism as a subtree (ADOPTION.md step 7) has no such
  file: `agent_os/` is a plain subdirectory of the host's own checkout, and only the host root
  carries `.git`. `git ls-files` resolves the enclosing repository by walking up from `cwd`,
  lists only what lies under `root` and names it relative to `root`, so the premise the test
  needs is "`root` is inside a git work tree", not "`root` is one": a `root` outside any
  repository still fails loudly, on git's own error. An empty listing now fails as well — a
  `root` its repository ignores lists nothing with exit 0 and would have passed without reading
  a file. Seen on titanarq/studentassistant#229 and in teachermovies:
  `test_no_host_literal_anywhere_under_agent_os` and `test_every_exclusion_still_applies` raised
  `RuntimeError: no .git under .../agent_os` on every subtree host. Both hosts deselected the two
  tests in their `ci-agent-os.yml`, a patch `agent-os-install` overwrites on every reinstall.
  Host follow-up: `git subtree pull`, then reinstall (or drop the `--deselect`s by hand).

- agent-os#70 (PR #81) — a GitHub rate limit is diagnosed before it is retried, and `issues.py
  validate` reads over REST. The "API rate limit already exceeded for user ID ..." a host read as a false
  positive is GitHub's answer for an empty **GraphQL** bucket, a quota separate from the REST
  `core` one that `gh api rate_limit`'s top-level `.rate` reports (read `.resources.graphql`).
  `gh_text` now asks `gh api rate_limit` (free) on a primary rate limit: a bucket at zero exits at
  once naming it, its limit and its reset time, instead of sleeping 31 s against a quota that
  refills up to an hour later; with quota left the refusal is retried as before. A secondary rate
  limit waits at least the minute GitHub asks for, and a bare 403 that is not a rate limit fails
  on its first answer instead of being retried five times. `validate` — run by `worker_task.sh
  start` before every dispatch — reads the issue with a REST `GET` and the open issues with the
  paginated REST listing (pull requests left out), so an empty GraphQL bucket no longer stops
  every dispatch. ADR `2026-09-25-a-rate-limit-is-diagnosed-against-the-logins-quotas-before-it-is-retried.md`.

- agent-os#75 (PR #80) — `worker_task.sh start` and `branch` no longer refuse the next dispatch
  over a finished run's other untracked files under `scratchpad/`, with or without a diary. #18
  archived the diary only when it was the one dirty path. A run that left an ad hoc script, a
  commit message draft and a `__pycache__/` there, with no `progress.log` at all (the reported
  case), kept every dispatch to that backend blocked until a human moved the directory out. Now,
  when `.state` records the run's ending, nothing is alive, and every dirty path is an untracked
  file under `scratchpad/`, all of them are archived to `.cache/diaries/`: the diary under #18's
  name `<run>.progress.log`, the rest under `<run>.scratchpad/` with their relative paths. These
  still refuse and move nothing: anything dirty elsewhere, a modified tracked file under
  `scratchpad/`, a run with no recorded ending, and `resume`. `retire_finished_runs_diary` is
  renamed `retire_finished_runs_scratchpad`. Host follow-up: nothing.

- (PR #79) — `templates/ci-agent-os.yml` is path-filtered to `agent_os/**` and to itself, as the
  2026-09-24 ADR, `AGENT_OS.md` §2.4, `ADOPTION.md` step 20, `install.py`, `doctor.py` and `lib.py`
  already said it was: the template had a bare `pull_request:` trigger and ran the mechanism's whole
  suite on every host PR, and `agent-os-doctor` counted it as the workflow that reports on every
  pull request, so a host without `ci-host.yml` passed the check the ADR meant it to fail. A test
  pins the filter and that the doctor does not count the file. `ADOPTION.md` step 20 adds: never
  make its job a required status check, since it does not run on a host-only PR. Host follow-up: a
  host with the old file gets the filter from `agent-os-install --force` after the subtree pull, or
  adds the `paths:` block by hand; keep `ci-host.yml` (or an unfiltered CI of your own) in place
  first.
- agent-os#72 (PR #77) — a refiner doubt the human already answered no longer comes back. The
  guard's `refine_pending` named every `status:refine` issue whose body fails the template, and a
  feature the refiner split never conforms: once the human answered its doubt and put
  `status:refine` back, every idle wake named it and the planner, never refining twice, parked the
  same answered question again. `refinable_issues` now lists the refine queue with its comments and
  drops an issue where the human commented after the latest `<!-- refiner-summary -->`
  (`agent_os.lib.refiner_pass_answered_by_the_human`); an unanswered summary is still named. The
  planner prompt says so, and the control plane's Duty 2 answers a split feature's doubt by lifting
  `blocked-on-human` only, never by restoring its refine label. Amends the 2026-09-15 refiner ADR.
- (PR #78) — `docs/ADOPTION.md` step 16 points `git subtree pull` at step 25, where the pull now is,
  instead of step 22 (arming the guard timer); every other "step N" cross-reference in `docs/`, the
  prompts, the templates and the code was checked against the current numbering (1-26) and holds.
  `templates/ci-agent-os.yml` cites `AGENT_OS.md` §5 step 7, the step that describes
  `agent-os-install`, instead of step 6. This section is reordered newest first, each entry naming
  the pull request that landed it, and the stray blank lines of parallel merges are gone.
- (PR #76) — the JavaScript actions every workflow uses move off Node 20, which GitHub is
  deprecating for actions: `actions/checkout@v4` -> `@v7` and `actions/setup-python@v5` -> `@v7`,
  both declaring `runs.using: node24` at their major tag (`checkout` runs on Node 24 from v5,
  `setup-python` from v6; v7 is the current major of each). Applies to this repository's
  `.github/workflows/ci.yml` and to `templates/ci-agent-os.yml` and `templates/ci-host.yml`, which
  `agent-os-install` writes into a host. Neither v7 removes anything these workflows use
  (`setup-python` v7 drops the `pip-install` input; `checkout` v7 refuses fork PRs only under
  `pull_request_target` and `workflow_run`). A self-hosted runner needs a runner version that ships
  Node 24. A host already installed keeps its old copies: after the subtree pull, `agent-os-install
  --dry-run` shows the diff, and `agent-os-install --force` rewrites them -- `--force` overwrites
  every file that differs, so a host that adapted its `ci-host.yml` edits the two `uses:` lines by
  hand instead.
- agent-os#52 (PR #74) — the guard's stall bookkeeping (`.cache/agent_guard_<backend>.json`) is
  scoped to the worker process it was counted in. The file now records `run_identity` (issue, start
  ref and PID of the live run); a tick whose run differs resets `commit_count`,
  `turn_count_at_commit` and `warned_at_turn_count` and keeps `last_quota_status`, so a new dispatch
  or a resumed stage is no longer measured from the previous run's anchor -- on Qwen that gave
  negative turns since commit, a late warn/cut, and a cut of a worker that had just committed. A
  file without the field counts as another run's. `turns_since_commit` also re-anchors at the
  process's start instead of going negative when the anchor is past its turn count, and a commit no
  longer drops the quota memory the same tick compares against. Claude's path counts from the live
  stream's own timestamps and never had the flaw.
- agent-os#27 (PR #73) — `issues.py move` no longer spends the user's shared GraphQL quota in
  proportion to the board. The board's `Status` field came from `gh project view` + `gh project
  field-list` on every move (~106 points on a 91-item board; one no-op move measured ~224), so a
  70-issue batch exhausted the 5000-points-an-hour quota every host shares. It is one bounded
  `gh api graphql` query now, answered once per process; the issue is read with a REST `GET`
  instead of `gh issue view`, and its one target label is checked with a REST `GET
  repos/{o}/{r}/labels/{name}` (created only on a 404) instead of `gh label list` —
  `ensure_fixed_labels` lists labels over REST too. A move costs three GraphQL requests of ~1
  point, independent of the board's size. New bulk form, backward compatible:
  `issues.py move 2 3 4 refine` resolves the label and the board field once, goes on past an
  issue that fails and exits non-zero naming it; one number prints exactly what it printed before.
- agent-os#54 (PR #69) — `agent-os-doctor`'s "labels that do not autocreate" check no longer
  requires `status:ai-completed`: it is a state label that `issues.py move` creates on first use, so
  a fresh host that has not yet completed a task passes. The check requires `status:agents-paused`,
  `auto-ready` and `wake:planner`, and `AGENT_OS.md` §4.3/§6 and `ADOPTION.md` steps 12 and 23 list
  only those three as labels to create by hand.
- agent-os#53 (PR #68) — `tests/test_no_host_literals.py` scans the files git knows (tracked, plus
  untracked-but-not-ignored) instead of walking the filesystem, and fails loudly when the root has
  no `.git` or `git ls-files` fails. Nested agent worktrees under `.claude/worktrees/` and ignored
  caches are no longer read; `.claude/worktrees/` and `.cache/` are now in `.gitignore`.
- agent-os#66 (PR #67) — the suite no longer registers worktrees in the repository that runs it. The
  launch-path tests of `tests/test_agent_task.py` ran the real driver with the checkout under test
  as its host, so every `git worktree add` wrote a registration into the real repository's gitdir,
  and a run that never reached its own removal left `/tmp/pytest-of-*/…/worktree-pr352-*` entries
  in `git worktree list` for every checkout. `launch_environment` now runs the driver out of a
  disposable git copy of the host (`AGENT_OS_HOST_ROOT` named outright), and the stand-in host of
  `tests/test_role_run_environment_isolation.py` is a repository of its own instead of a `.git`
  file naming the real gitdir; its heads are committed there too. Both files assert where the
  worktree was registered. Tests only; no driver behaviour changed.
- agent-os#50 (PR #65) — a PR whose head SHA reports zero checks now explicitly fails the control
  plane's merge condition 1 (`agents/control-plane.md` duty 4, `docs/AGENT_OS.md` §2.4): it hands
  the PR back to the human instead of merging. So that no PR lacks a check, `agent-os-install` also
  renders `.github/workflows/ci-host.yml`, running `project.test_command` on every pull request with
  no path filter (new key `project.install_host_ci`, default `true`; written only if absent,
  `--force` to overwrite), and `agent-os-doctor` fails when no workflow fires on `pull_request`
  without a path filter. `ADOPTION.md` step 23: land the host's CI before the first product PR, and
  never make the CI task depend on a skeleton. ADR
  `2026-09-24-a-pr-with-no-checks-fails-the-ci-condition-and-every-host-ships-a-ci.md`.
- (PR #59) — the mechanism is released under the MIT License (`LICENSE`).
- agent-os#39 (PR #62) — a split no longer strands the original's dependents. `issues.py supersede N
  --by A --by B [--route D=A]` rewrites every open issue's `Blocked by #N` line to the children
  (all of them unless a route narrows one dependent), comments on each dependent, then comments
  `Superseded by #A, #B` on N and closes it as `not planned`; it refuses a feature (its children
  are its parts), a child that is not open, and is idempotent. The refiner prompt calls it after
  splitting a task or bug. Before, the original stayed open and its dependents blocked forever, or
  unblocked too early when a human closed it; a host rewriting dependents by hand can stop.
- agent-os#61 (PR #64) — `worker_task.sh open-pr` classifies a rejected push. GitHub's refusal to
  let an App without the Workflows permission create or update a ref whose tree differs from the
  default branch under `.github/workflows/` -- which a stale branch hits without touching a
  workflow, typically after `open-pr`'s merge with its base conflicted -- now writes `BLOCKED
  reason=workflows_permission` instead of attempting a fast-forward onto a remote branch that does
  not exist. Every rejected push, classified or not, now comments the rejection on the issue and
  moves it to `status:blocked-on-human`; it used to stay in `status:doing` with no pull request and
  nothing visible. A later successful `open-pr` rewrites a leftover `BLOCKED` line 1 of `.state` to
  `DONE`. Hosts whose worker Apps lack Workflows permission: see `ADOPTION.md` §3.
- agent-os#41 (PR #63) — a fresh worktree is provisioned the way the host configures it, not by a
  hard-coded root `.venv`/`.env` link: `project.worktree_links` (default `[.venv, .env]`, the old
  behaviour) are symlinked from the main checkout, then `project.worktree_setup_command` (default
  empty) runs inside the worktree, for both the validator's throwaway worktree and
  `worker_task.sh <backend> init` (one shared helper, `agent_provision_worktree`). A setup that
  exits non-zero refuses the run -- no validator is launched, `init` removes the half-made tree
  and its branch -- instead of handing an agent an empty tree. The validator's prompt no longer
  claims the worktree is "already populated", its lint bullet renders from the new
  `project.lint_commands` (default empty: no bullet; `config.example.yaml` keeps the two `ruff`
  commands), and the shared-database pytest warning and the "~50 minutes" suite duration are gone
  (a host that needs the warning puts it in `never_run` or its validator `prompt_extras`). Closes
  §7 row (aa). Host follow-up: a monorepo sets `worktree_setup_command` to its own bootstrap
  (e.g. its `uv sync --frozen` / `npm ci`); a host that relied on the validator's ruff bullet sets
  `lint_commands`.
- agent-os#35 (PR #46) — in a host that vendors the mechanism under `agent_os/`, a role's worktree
  now runs the worktree's copy of the mechanism, not the main checkout's. `PYTHONPATH=<worktree>`
  alone left `<worktree>/agent_os/` as a namespace portion (it has no `__init__.py`), so the regular
  package the mechanism venv's editable `.pth` puts on `sys.path` won, and a validator testing a
  `subtree pull` certified code it never ran. The drivers now export
  `PYTHONPATH=<worktree>:<worktree>/agent_os` there (unchanged where the mechanism is the repository
  root) and link `agent_os/.venv` into the worktree beside the root `.venv` and `.env`; a worker's
  persistent worktree gets the link only where git ignores it. The mechanism's `.gitignore` names
  `.venv` without the trailing slash so that link is ignored. The worktree isolation test measures
  the mechanism's own package in such a host instead of skipping.
- agent-os#32 (PR #56) — `refine_pending` names the head of a ranked refine queue instead of the ten
  newest refine-needing issues: `guard.refinable_issues()` sorts by `lib.refine_queue_rank` —
  parent carries `labels.auto_ready` first, then the issue's best label in `labels.priorities`
  (none sorts last), then no open `Blocked by #N`, then issue number ascending. The parent's
  labels are read once per distinct parent, through the same lookup `promote_refined` now
  shares. The planner prompt says the list is in that order and to launch the refiner on the
  earliest listed issue that passes its summary check. A host that parked refine-needing issues
  to steer the order (studentassistant's `scripts/refine_window.py`) can drop that after the
  subtree pull.
- agent-os#15 (PR #60) — `issues.py create --type task|bug` (and `--template`) now puts the `title:`
  of that type's `.github/ISSUE_TEMPLATE/<type>.md` front matter (`[task] `, `[bug] `) in front of
  the given title. An issue created through the API skips GitHub's form, which is what adds the
  prefix to a hand-written one, so the refiner's sub-issues came out without it. A title that
  already starts with the prefix (case-insensitive, with or without its space) is left alone, so
  `[task] [task] ` cannot happen. A type with no template, or a template with no `title:`, keeps its
  title.
- agent-os#37 (PR #55) — `agent_guard.py tick` no longer dies on a stream event whose top-level
  `message` is a string (Claude Code's `system/permission_denied`, written when it refuses a tool
  call): the parsers, the guard's loop detector and the drivers' inline readers take `message` as an
  API message only when it is an object, and `read_events` drops any line that parses to something
  other than an object. One refused command used to stall every tick -- no promotion, no liveness,
  no quota verdict -- until its log aged out of the quota window. A host that worked around it (a
  `sanitize_role_logs.py` `ExecStartPre` rewriting the key) can drop the workaround.
- agent-os#33 (PR #58) — the planner, validator and refiner get a scratch directory of their own:
  each run is handed `AGENT_RUN_SCRATCH`, an empty `mktemp -d` directory outside `.cache/<role>/`
  and the checkout, which the driver removes when the run ends; the three prompts name it and
  forbid writing, moving or deleting anything under `.cache/`. A refiner had written its drafts
  into `.cache/refiner/` and then `rm -rf`'d it, deleting the run log, the PID file `role_died`
  detection reads and every `runs.tsv` row. A host's `prompt_extras` telling a role to use
  `mktemp -d` is no longer needed.
- agent-os#51 (PR #57) — the guard unit's `ExecStart=` no longer prefers a `.venv` at the host root
  when the host has a `scripts/agent_guard.py` shim: the shim, like the module form, now always runs
  on the mechanism's own interpreter (`unit_python()`), as AGENT_OS.md §8 already said, so the
  host's package versions cannot change how the guard behaves. The shim only re-executes
  `agent_os.guard` on that same interpreter, so nothing it needs came from the host's venv. A host
  whose unit was rendered with the host's `.venv` keeps it until it re-renders: run
  `agent-os-install --dry-run` to see the diff, then `agent-os-install --force` (which also rewrites
  the other installed templates) and `systemctl --user daemon-reload`.
- agent-os#23 (PR #47) — `issues.py create` now adds the new issue to `project.board_number`
  (`gh project item-add`) and, when it is created with a state label (`status:ready`, …), sets
  the column `project.board_columns` maps that state to on the item it just added. Before, a later
  `move` found no item to mirror onto unless the board auto-added issues. A board that refuses
  either step never fails the `create`: it prints one `board:` line saying why. `create` with
  `board_number: 0` makes no board call.
- agent-os#10 (PR #44) — the guard-timer failure `agent-os-doctor` prints no longer sends a host to
  `docs/runbooks/agent_monitor.md`, a runbook only the first host ever had. It now says what to
  run (`systemctl --user enable --now <guard_unit>.timer`, once `agent-os-install` has written the
  unit) and names `agent_os/docs/ADOPTION.md` steps 20 and 22. The same dead reference is gone
  from the rendered `override.conf`'s comment and from the `doctor`/`install` docstrings. This
  also closes the second point of agent-os#5.
- agent-os#13 (PR #49) — `docs/ADOPTION.md` step 7 spells out the `git remote add` + `git subtree
  add` commands and says when git needs a credential of its own. `titanarq/agent-os` is public now,
  so the add and every `subtree pull` need none. A `subtree push`, or any fetch from a private fork
  or mirror, needs one, and a `gh` login alone does not give it to git. The step gives `gh auth
  setup-git` as the fix, and an SSH remote as the alternative. Prose only: nothing in the mechanism
  runs this step, so there is no behaviour for a test to pin.
- agent-os#5 (PR #42) — `agent-os-doctor`'s board check no longer passes on a Project that is not
  the repository's: besides the `Status` field and its options, it reads the Projects linked to
  `project.repo` (`repository.projectsV2`) and fails when `project.board_number` is not one of them,
  naming the linked ones and the `gh project link` that would fix it. A `board_number: 1` copied
  from the example used to pass against whatever the owner's Project 1 was.
- agent-os#8 (PR #48) — `docs/ADOPTION.md` names the live config keys: step 14 puts a worker's App
  at `project.backends.<name>.app` instead of the deprecated `project.worker_apps.*`, and step 18
  runs `worker_task.sh <backend> init` once per `project.backends` entry with a `worktree` instead
  of per `project.worktrees` entry. `docs/AGENT_OS.md`'s actors table and §4.3 say the same. A test
  in `tests/test_backends_config.py` walks every `project.`/`mechanism.`/`planner.` key ADOPTION.md
  mentions through the config schema and fails on one the loader does not have or warns about as
  deprecated.
- agent-os#11 (PR #43) — the role prompts name no script under a host's own `scripts/`. The worker's
  HOW TO REPORT no longer sends every worker to `scripts/debug.py`, a file one host has: it asks
  for a breakpoint, a debugger session or a throwaway print, with the project's own debugging tool
  when the host's `project.prompt_extras` paragraph names one. The validator's evidence example
  spells the test runner as `__TEST_COMMAND__`, so it renders the host's `project.test_command`
  (the example config's `scripts/test.sh` renders word for word as before). `tests/golden/worker.md`
  changes by that one sentence. A test in `tests/test_prompt_templates.py` fails on any
  `scripts/<path>` in a template.
- agent-os#7 (PR #45) — `docs/ADOPTION.md` step 12 lists `wake:planner` among the labels a host
  creates by hand, next to `status:ai-completed`, `status:agents-paused` and `auto-ready`: those
  four are exactly what `agent-os-doctor` checks for, and a host that followed the old list failed
  the doctor's label check on its first run. Step 23 now says four labels, not three.
  `tests/test_adoption_doc.py` asks `doctor.check_labels` which labels it requires and fails when
  step 12 leaves one out.
- agent-os#4 (PR #40) — `agent-os-doctor` reports every check even when `gh` fails inside one of
  them: a failed `gh` call (a `project.repo` that does not exist, say) or a `gh`/`systemctl` that is
  not installed turns that check into a `[FAIL]` carrying the error on one line, and the run
  continues instead of exiting after the labels check.
- agent-os#22 (PR #29) — `worker_task.sh resume` no longer refuses to relaunch a run the guard cut
  over that run's own diary: when `.state` line 1 records `CUT_BY_GUARD` and nothing is alive, the
  dirty check leaves out `scratchpad/progress.log` (` M` when tracked, `??` when untracked, and the
  collapsed `?? scratchpad/` only while the diary is the one file inside it). The file stays on disk
  untouched, for the monitor and the resumed run. Any other dirty path, and a diary over any other
  `.state` (`STARTED`, `RESUMED`, `DONE`, `FAILED_LAUNCH`, `BLOCKED`), still refuse. A cut followed
  by `resume` no longer needs a host's `info/exclude` entry for the diary.
- agent-os#17 (PR #34) — the mechanism's own suite passes in a host that vendors it through `git
  subtree` whatever that host's layout: `test_agent_task.py` no longer asserts the host root holds
  exactly one top-level package and its own `.venv`. The worktree isolation test measures the first
  package the host's venv installs (of zero, one or several), and is skipped with its reason when
  the host has none, as a non-Python host does; only the mechanism's own repository still requires
  one. A host's `--deselect` workaround for the two tests can go after its next `subtree pull`.
- agent-os#6 (PR #24) — `pyproject.toml` depends on `PyJWT[crypto]>=2.8` instead of a bare
  `PyJWT>=2.8`: without the extra, the interpreter `bootstrap.sh` builds has no `cryptography`,
  PyJWT registers no RS256 signer, and `gh_app_token` fails with `KeyError: 'RS256'` signing the App
  JWT. A host that installed `cryptography` by hand as a workaround can drop that step.
- agent-os#3 (PR #38) — `agent-os-install` and `agent-os-doctor` no longer crash with a traceback
  when `config/agents.yaml` is missing, is not YAML or does not match the schema. `install` exits 1
  with one line naming the file (and, when it is missing, `ADOPTION.md` step 8); `doctor` reports it
  as a failed `config/agents.yaml loads` check and still runs the two checks that need no config
  (python version, `gh auth status`).
- agent-os#14 (PR #26) — `issues.py move` finds the issue's board item from the issue's own
  `projectItems` (one GraphQL query matching board number and owner) instead of listing the whole
  board with `gh project item-list`, which returned zero items for an org Project v2 that held them
  and so skipped every column mirror in silence.
- agent-os#12 (PR #31) — `agent-os-install` no longer renders a bare `python3` into the guard unit's
  `ExecStart=` when the host has a `scripts/agent_guard.py` shim but no root `.venv`: it uses the
  mechanism's own interpreter as `agent_os_python()` resolves it (`$AGENT_OS_PYTHON`, else the
  `.venv` `bootstrap.sh` builds), and refuses with a message naming `bootstrap.sh` and
  `AGENT_OS_PYTHON` when that is not an absolute path to an executable, in the module form too.
  Nothing is written in that case.
- agent-os#9 (PR #36) — `agent-os-install` no longer prints "would create" for a file it actually
  creates. Only `--dry-run` speaks in the conditional ("would create", "would overwrite
  (--force)"); a real run reports "created" and "overwritten (--force)", printed after the write.
  "up to date -- skipped" and "refusing without --force" are true in both modes and unchanged.
- agent-os#16 (PR #30) — `worker_task.sh start`'s base-branch gate accepts any lowercase `<word>`
  before `/<issue>-<slug>`, hyphens and digits included: `agent-os/37-gradle-skeleton` passes for
  #37, where `[a-z]+` refused it. The anchoring on the number is unchanged, so
  `task/387-close-the-390-gap` still fails for #390, and the refusal now names the accepted shape
  (`<word>/<issue>-<slug>`) instead of "a branch naming #N".
- agent-os#25 (PR #28) — the suite no longer leaves a `.cache/` in the checkout it runs from. The
  `rules` subcommands of `worker_task.sh` and `planner_task.sh` stop creating their cache
  directories. `test_agent_task.py`'s no-verdict cache moves out of the real `.cache`, and the
  `start` refusal tests get a disposable `WORKER_CACHE_DIR`. A conftest guard now fails any test
  that creates `<root>/.cache` when the session started without one.
- agent-os#18 (PR #20) — `worker_task.sh start` and `branch` no longer refuse the next dispatch over
  the previous run's diary: when `.state` records that run's ending (`DONE`, `CUT_BY_GUARD`,
  `FAILED_LAUNCH`, `BLOCKED`), nothing is alive and the untracked `scratchpad/progress.log` is the
  only dirty path, it is archived to `.cache/diaries/` and the dispatch proceeds. A diary whose run
  never recorded an end, any other leftover, and `resume` still refuse as before. A host's
  `info/exclude` entry or cleanup script for the file is no longer needed.
- #513 (this task) — the mechanism's own docs move under `agent_os/docs/`: `AGENT_OS.md` (from
  `docs/AGENT_OS.md`), its ADRs (from `docs/adr/`, dated 2026-09-14 through 2026-09-18 and
  2026-09-21), `ADOPTION.md` (the export recipe of §5 as a numbered checklist for a second host)
  and this file.

Wave 3's other two tasks, #429 (which backend fallback rule to write down as an ADR) and #514
(optional), add their own entries here once their pull requests merge.

## 2026-09-22

- #512 (PR #525) — the mechanism's own suite (`agent_os/tests -q`) now runs from a copy made
  outside the repository, as a CI step, proving nothing under `sys.path` still resolves through
  roedor's editable install; roedor's own values, including the m2-fingerprint assertion the move
  had left unwatched, moved into `tests/test_agents_config_conformance.py` on the host side.
- #510 (PR #524) — `worker_progress.sh` and `gh_app_token.py` stop hardcoding roedor's backend
  names and `secrets_dir`; the two `.claude/agents/*.md` templates and the `status:*`/`type:*`/
  `p1..p4`/`module:` label vocabulary move to config; a walk test refuses a roedor literal inside
  `agent_os/` from landing again.
- #511 (PR #523) — `agent-os-install [--dry-run] [--force]` writes the systemd units and copies the
  first-run templates; `agent_os/bin/worker_task.sh <backend> init` creates a backend's worktree
  idempotently; `agent-os-doctor` reads the whole first-run checklist back in one pass: adopting
  the mechanism on a machine becomes three commands instead of a hand-run checklist.
- #509 (PR #522) — the four role prompts (worker, validator, refiner, planner) become template
  files under `agent_os/prompts/`, rendered by one function with a single marked extension point,
  `__PROJECT_EXTRAS__`, for a host's own text; a golden test proves all four render word for word
  what the inline strings they replaced used to.

## 2026-09-21

- #508 (PR #520, PR #521) — the mechanism becomes one directory, `agent_os/`: its own
  `pyproject.toml`, its own interpreter (`agent_os/bootstrap.sh` → `agent_os/.venv`), its own tests
  with their own `conftest.py`. roedor's old entry points under `scripts/` and the tracker CLI
  become one-line shims that `exec` into it. PR #521 lands the root `AGENTS.md` paragraph naming
  `agent_os/` separately, because that file sits inside `project.forbidden_paths` and the merge
  gate's condition 3 audits it.
- #476 (PR #516) — the merge gate's condition 3 (nothing outside a delivery directory touched)
  used to audit the whole `project.forbidden_paths` list; `project.merge_audit_exempt_paths` now
  subtracts the delivery directories from what gets audited, so a diff that only adds a file under
  one of them no longer trips the condition it was never meant to guard.
- #443, #435, #483 (PR #519) — three worker-driver bugs in one PR: a `start` that found nothing to
  launch no longer emits a `worker_finished` event; `branch` with no base fetches and forks from
  `origin/main` instead of a stale local one; the usage report reads the issue's own budget class
  instead of a fixed ceiling.
- #426, #436 (PR #518) — two planner-wake bugs: a backend rejecting a wake no longer consumes the
  `idle_dispatchable` rate-limit window it was going to use productively later; a `new_dispatchable`
  event found while every worker slot is occupied is retained instead of dropped.
- #428, #419 (PR #517) — the guard survives a worker's `cutoff=` line that fails to parse (prints
  it, keeps ticking) and judges liveness against the mtime it actually observed arriving, not the
  timestamp the worker typed into the line.
