#!/usr/bin/env bash
# Drives a headless WORKER agent -- Qwen Code or Claude Code -- in its own git worktree: launch,
# watch its context growth, collect. Generalized on 2026-09-06 from `qwen_task.sh`, whose header
# still explains the four mechanisms (PYTHONPATH, the PID lock, the ownership audit, the context
# budget); nothing about them changed, they just apply to two backends now.
#
#   agent_os/bin/worker_task.sh <qwen|claude> rules                        # the resolved RULES block
#   agent_os/bin/worker_task.sh <qwen|claude> init                         # create the worktree if absent
#     idempotent: a worktree already there is left alone. `git worktree add` on a fresh branch
#     from origin/main when there is none yet (#392, agent_os/docs/AGENT_OS.md §7 row (r)).
#   agent_os/bin/worker_task.sh <qwen|claude> branch <name> [<from>]       # fresh branch in that worktree
#     no <from>: fetches and branches from origin/main, refusing if the fetch fails (#435) --
#     an explicit <from> is honoured verbatim and fetches nothing.
#   agent_os/bin/worker_task.sh <qwen|claude> start <issue> [extra-brief.md] [--force]
#     refuses, writing nothing, at `planner.max_parallel_issues` workers already alive across
#     every backend, or when an alive worker's own issue shares a `module:` label with this one
#     (#374): the driver enforces both, the planner only reads the refusal.
#   agent_os/bin/worker_task.sh <qwen|claude> status                       # alive? context? last lines
#   agent_os/bin/worker_task.sh <qwen|claude> watch                        # follow the events
#   agent_os/bin/worker_task.sh <qwen|claude> collect                      # commits, audit, cost
#   agent_os/bin/worker_task.sh <qwen|claude> open-pr                      # push the branch, open the PR
#   agent_os/bin/worker_task.sh <qwen|claude> stop                         # by PID, never by pattern
#   agent_os/bin/worker_task.sh <qwen|claude> freeze <reason>              # freeze what a run left
#     uncommitted, in a `WIP: cut by guard (<reason>)` commit -- the door `agent_guard.cut_run`
#     reaches instead of keeping a freeze of its own (#482).
#   agent_os/bin/worker_task.sh <qwen|claude> resume [<brief.md>] [--after <quota|guard_cut|manual>]
#                                                 [--context "<text appended to the instruction>"]
#     refuses at `planner.relaunch_cap` `WIP: cut by guard` commits since the branch's fork point
#     from its base (#362), of which `open-pr`'s pre-merge freeze is not one (#407): no pid, no
#     state, no events are written on a refusal.
#   agent_os/bin/worker_task.sh <qwen|claude> stage-exit    # the driver's own end-of-stage decision
#   agent_os/bin/worker_task.sh <qwen|claude> launch-stage  # the next stage, no gates -- chaining only
#     Both are called by the run's own subshell, never by hand: `start` and `resume` are the doors.
#
# ONE STAGE PER PROCESS (#375, agent_os/docs/adr/2026-09-15-work-is-staged-before-dispatch-and-each-stage-
# runs-in-a-fresh-process.md). An issue's `## Stages` checklist is its plan; a branch's
# `stage N/M: <title>` commits are what has actually been done, and nothing else is ever consulted
# for progress. `start`/`resume` launch the FIRST INCOMPLETE stage and nothing more; when that
# process exits, `stage-exit` archives its spend, and either chains the next stage into a brand
# new process group or writes the run's terminal state for the planner. Running out of tokens
# therefore costs the current stage, never the task.
#
# THE ISSUE IS THE UNIT OF WORK (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-
# labels-are-the-mechanical-state.md). `start` takes a GitHub issue number and nothing else: it
# refuses an issue that `issues.py validate` rejects (unless --force), resolves the budget class
# its `<!-- budget: <class> --> ` line names in config/agents.yaml, assembles the issue body and
# its parent's into .cache/worker_<backend>.brief.md -- with the optional extra brief appended
# under `## Supplement` -- and moves the issue to `doing`. `resume` reuses that same file and the
# issue recorded by the `start` it is resuming.
#
# One worktree per backend, so both can run at once without touching each other's tree; the paths
# and the GitHub App slugs come from config/agents.yaml's `project:` section, never from here
# (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
# The database is NOT per backend. It is one Postgres for everyone, and a worker's own connection
# is read-only by default (`project.worker_environment`, below) as well as by the rules injected
# below -- an accident guard, not an intent guard, and DB tests reach the owner only through
# `project.test_command` (agent_os/docs/adr/2026-09-15-workers-connect-read-only-by-default-and-reach-the-
# owner-only-through-the-test-runner.md). The main thread is the only writer.
#
# The Claude worker spends the SAME Anthropic subscription window as the main thread. A long Opus
# run is not free just because it is not in this terminal: check `status` before starting a second.
set -uo pipefail

# The identity block is the same three steps for every role, so it is sourced from the one-shot
# driver rather than copied (sourcing defines the `agent_*` helpers and returns before its own
# dispatch), and with it come the resolved interpreter (`$agent_python`) and the HOST project's
# root, which this driver has always called `$main`.
# shellcheck source=agent_os/bin/agent_task.sh
source "$(dirname "${BASH_SOURCE[0]}")/agent_task.sh"
main=$agent_main

# One field of config/agents.yaml's `project:` section; `--path` resolves it against this
# checkout, which is what the `worktrees:` entries are relative to.
project_value() { "$agent_python" -m agent_os.lib project-value "$@"; }
# One capability of this driver's backend -- its `project.backends` entry (#514) -- so nothing
# below branches on the backend's NAME: exits 2 for a name that is not a configured backend, 1 for
# a config that does not load.
backend_value() { "$agent_python" -m agent_os.lib backend-value "$@"; }

# The main checkout, derived from git rather than $main: this script's own file can be reached
# from inside a worktree too (every worktree has its own copy of agent_os/bin/worker_task.sh), and in
# that case $main would resolve to the worktree, not the checkout the RULES below need to name.
# The shared .git directory always lives in the main checkout, worktree or not.
#
# `git rev-parse` failing (no `.git` anywhere above $PWD) is captured and checked explicitly:
# under `set -uo pipefail` -- no `-e` -- an unchecked `dirname "$(failing-command)"` would still
# exit 0 (dirname succeeds on the empty string it gets), so the caller's `|| exit 1` never fires
# and a half-derived "." reaches the rendered RULES instead of stopping the driver. The same bug
# `capture_golden.sh`'s own `main_checkout` resolution was fixed against (#512).
main_checkout() {
  local common_dir
  common_dir=$(git rev-parse --path-format=absolute --git-common-dir) || {
    echo "main_checkout: $PWD is not a git repository" >&2
    exit 1
  }
  dirname "$common_dir"
}

backend=${1:-}
case "$backend" in
  '' | -*) sed -n '2,35p;37,44p' "$0"; exit 2 ;;
esac
# The backend CLI this driver launches, resolved from `project.executables` rather than from
# whatever PATH the process that launched the driver happened to carry: a dispatch from the
# systemd user unit and a dispatch from a shell must run the SAME binary (#380). A backend the
# project does not configure resolves to its own bare name, which is the PATH lookup this driver
# has always done. Also what `stage-exit` names when the shell could not find it at all (#381).
# A config that does not load exits non-zero here and stops the driver: see `agent_executable`
# (agent_os/bin/agent_task.sh) for why an unchecked status is how an EMPTY command gets launched.
backend_bin=$(agent_executable "$backend") || exit 1
# Which parser reads this backend's events, and with it which command-line dialect launches it
# (`launch_stage` below). A name that is not a configured backend is a usage error, exactly as an
# unknown name in the hardcoded pair this replaced was (#514).
backend_stream=$(backend_value "$backend" stream)
case $? in
  0) ;;
  2) sed -n '2,35p;37,44p' "$0"; exit 2 ;;
  *) exit 1 ;;
esac
case "$backend_stream" in
  qwen_jsonl | claude_jsonl) ;;
  *)
    echo "backend '$backend' reads as stream '$backend_stream', and this driver knows no command line that writes it" >&2
    exit 1
    ;;
esac
# The model when the dispatch names none: the first worker class on this backend, else the first
# class of any role on it -- read off the classes, never a per-backend literal here (#514). Left
# empty when no class runs on the backend; `launch_stage` refuses to start on an empty one.
model=${WORKER_MODEL:-$("$agent_python" -m agent_os.lib backend-model "$backend" 2>/dev/null)}
worktree=${WORKER_WORKTREE:-$(backend_value "$backend" worktree --path)}
shift

# `WORKER_CACHE_DIR` moves every one of these at once, and `agent_guard.py`'s `cache_dir()` reads
# the same variable for the paths IT derives -- `worker_paths()`, `planner_events/`, `wake`'s
# `planner.lock` -- so one value isolates the driver and the guard together and a test can drive
# this script, exit hook included, without writing over the state of a run that is actually alive.
cache=${WORKER_CACHE_DIR:-$main/.cache}
pidfile=$cache/worker_$backend.pid
logfile=$cache/worker_$backend.log
events=$cache/worker_$backend.jsonl
startref=$cache/worker_$backend.startref
sidfile=$cache/worker_$backend.session
statefile=$cache/worker_$backend.state
issuefile=$cache/worker_$backend.issue
brieffile=$cache/worker_$backend.brief.md
# The issue's own body, written out because `agent_lib stage-titles` reads the stages off a file,
# and `N/M` -- stages already committed when THIS process was launched, out of the total the issue
# declares. `stage-exit` compares the count it derives from git against that N to tell a stage
# that landed from a process that produced nothing (#375).
bodyfile=$cache/worker_$backend.body.md
stagefile=$cache/worker_$backend.stage
# One finished stage process's event stream, per issue, kept where #367's spend report will read
# it: .cache/spend/<issue>/<ts>-<backend>-stage<N>.jsonl.
spenddir=$cache/spend
mkdir -p "$cache"

# The worker's own liveness diary, relative to the worktree root -- the same path the guard reads
# (`agent_guard.py`'s `worker_paths`) and the one the RULES below tell the worker to append to. It
# is the one file a worker writes that NO commit may carry (#407): it is not the task's output, it
# reached `main` inside a pull request and every worker branch after that conflicted with `main` on
# it, and its uncommitted lines are the dirty-worktree signal `start`, `resume` and `branch` refuse
# to relaunch over. Relative, so it is a pathspec `git add`, `git diff` and `git log` all take.
DIARY=scratchpad/progress.log

# The subject every freeze this driver writes, and the ONE freeze that is not a cut (#407):
# `open-pr` freezes the tree before it merges the base in, when the run has finished every stage
# and nothing was cut. Both readers of the subject -- `resume`'s relaunch count and `has_wip_after_
# last_stage_commit` -- go through `subject_is_a_guard_cut`, which excludes it, so a run that
# reached its pull request spends no attempt of `planner.relaunch_cap` and the next stage is not
# told to build on work that is in fact finished (#369's 81bfae4 spent an attempt it never used).
# Reason and subject are defined together because they have to keep matching: the writer names the
# reason, the readers recognise the whole subject.
WIP_SUBJECT='WIP: cut by guard'
PRE_MERGE_FREEZE_REASON=before_merge
PRE_MERGE_FREEZE_SUBJECT="$WIP_SUBJECT ($PRE_MERGE_FREEZE_REASON)"

# Driver-written, line 1: the guard's ground truth, knowable from this script's own bookkeeping
# with zero cooperation from the agent (agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-
# writes-cooperative-state.md). Line 2, when present, is the issue/label marker `write_state_
# marker` below writes -- every writer of line 1 preserves it (#350 Part 4).
write_state() {
  local line2
  line2=$([ -s "$statefile" ] && sed -n '2p' "$statefile" || true)
  if [ -n "$line2" ]; then
    printf '%s\n%s\n' "$*" "$line2" > "$statefile"
  else
    printf '%s\n' "$*" > "$statefile"
  fi
}

# Line 2 of .state: the issue and the status:* label this driver just wrote for it, so the guard's
# tick can compare local state against GitHub and log drift (#350 Part 4) -- never written before
# the `issues.py move` it records has actually succeeded. Preserves whatever line 1 currently says
# (STARTED/RESUMED/DONE/CUT_BY_GUARD, or a leftover from a previous run about to be overwritten by
# this same `start`/`resume` call).
write_state_marker() {
  local issue=$1 label=$2 line1
  line1=$([ -s "$statefile" ] && sed -n '1p' "$statefile" || true)
  printf '%s\nissue=%s label=%s\n' "${line1:-STARTED}" "$issue" "$label" > "$statefile"
}

# A fresh `start` must never inherit the previous run's issue/label marker: if `move doing` below
# fails, `write_state STARTED` would otherwise preserve line 2 as-is, and the tick would report
# false drift (case a) against an issue this run has nothing to do with -- typically the previous
# run's own issue, already moved on to review/done by then. Drops line 2, keeps line 1 exactly as
# it is (about to be overwritten by `write_state STARTED` anyway). `resume` reuses the same run's
# own marker and never calls this.
clear_state_marker() {
  [ -s "$statefile" ] || return 0
  local line1
  line1=$(sed -n '1p' "$statefile")
  printf '%s\n' "$line1" > "$statefile"
}

# Does the issue's own body name this path, literally? Only the second list below ever asks: a
# mechanism file is off limits unless the body names the path as the target of the work, and
# "names" is a substring, not a glob and not a regex, so the check cannot be wider than what the
# human wrote. Read off the body this run already fetched into `$bodyfile` -- never a second `gh`
# call (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md).
issue_body_names_path() {
  [ -s "$bodyfile" ] || return 1
  grep -qF -- "$1" "$bodyfile"
}

# The paths the agent must never write, as the `grep -E` pattern `collect` audits a run's changed
# paths with. Built from `project.forbidden_paths`, which is also what renders the "FILES YOU MUST
# NOT TOUCH" paragraph injected below -- ONE list behind both halves of that rule, so the prose the
# worker reads and the audit that judges it cannot describe two different sets again
# (agent_os/docs/AGENT_OS.md §7 rows (a) and (s), issue #363). Empty when the project forbids nothing, and
# then `collect` skips the audit instead of running an empty pattern, which matches every path.
FORBIDDEN=$("$agent_python" -m agent_os.lib forbidden-paths-regex)

# The mechanism's own files: the SECOND list `collect` audits with, and the only one an issue body
# can authorize. A path matching this pattern is refused when the body does not name it and allowed
# when it does, because these files are the machinery under development and the tracking epic
# exists to change them (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-
# protected-paths.md). Rendered by the same translator as `FORBIDDEN`, read with the same
# empty-means-no-audit rule, and the list the second of the two ownership paragraphs the worker
# reads is rendered from.
MECHANISM=$("$agent_python" -m agent_os.lib mechanism-paths-regex)

# Injected into every run, whatever the brief says. The brief is the task; this is the contract.
# The text is `agent_os/prompts/worker.md` plus whatever `project.prompt_extras.worker` appends at
# its extension point, and every paragraph that comes out of config/agents.yaml -- the two lists a
# worker's diff is audited against, the commands this project forbids, the environment it exports
# into the worker's own process -- is rendered by `agent_lib.render_prompt` rather than substituted
# here (#509). The pair of ownership paragraphs still renders whole or not at all, on the same test
# the `FORBIDDEN` and `MECHANISM` patterns above are built from, and that rule now lives beside the
# renderer instead of beside this call
# (#390, agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-projects-protected-paths.md).
#
# The main checkout's path is the one value only this run knows: derived, not configured -- see
# main_checkout() above. A prompt that will not render stops the driver here, because a backend
# launched on a half-written contract is worse than one not launched at all.
#
# Resolved into its own variable BEFORE the `--set` it feeds: `main_checkout`'s `exit 1` runs
# inside the subshell a nested `$(main_checkout)` would create, which only ends that subshell --
# nested inside another substitution's argument, its failure is never checked and render-prompt
# would run anyway, on an empty MAIN_CHECKOUT. As its own assignment, `$?` is `main_checkout`'s
# own exit status and `|| exit 1` stops this driver before render-prompt is even invoked.
main_checkout_value=$(main_checkout) || exit 1
RULES=$("$agent_python" -m agent_os.lib render-prompt worker \
  --set "MAIN_CHECKOUT=$main_checkout_value") || exit 1

# `alive_pidfile` takes an explicit path so `start` can also ask about backends other than this
# one (#374, the parallelism cap below) -- `alive` is this backend's own pidfile, unchanged.
alive_pidfile() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
alive() { alive_pidfile "$pidfile"; }

# Latest per-turn context size, cumulative output, turn count, session id -- read off the
# stream-json events. Delegates to agent_os.lib so there is exactly one implementation of
# "how big is a turn" / "did the run fail", shared with agent_os.guard's budget check.
# The context line is judged against the issue's OWN budget class, read off `$bodyfile` the same
# way `issue_token_line` below resolves `max_total_tokens` -- never the per-backend $max_context
# default, which read as a ceiling for every class alike and reported a healthy run as over budget
# (#483). The issue's own token total goes in under the per-stage context line, because the two
# are the same measure at the two scopes a run is judged at: `max_context` for this stage process,
# `max_total_tokens` for every stage the issue has taken (#387).
usage_report() {
  [ -s "$events" ] || { echo "  (no events yet)"; return; }
  "$agent_python" -m agent_os.lib usage-report "$events" "$bodyfile" |
    awk -v extra="$(issue_token_line)" \
      '{ print } extra != "" && /^  context/ { print extra; extra = "" }'
}

# The issue-wide line of that report, empty -- so the report shows only this stage's own numbers --
# when no issue is recorded, which is a run that was never staged. The ceiling is printed when the
# body on disk resolves to a class and left out when it does not: `start` has already refused a
# dispatch whose issue has no resolvable budget, and a report is not the place to relitigate that.
issue_token_line() {
  local issue ceiling
  issue=$(cat "$issuefile" 2>/dev/null || true)
  case "$issue" in
    ''|*[!0-9]*) return 0 ;;
  esac
  ceiling=""
  if [ -s "$bodyfile" ]; then
    ceiling=$("$agent_python" -m agent_os.lib resolve-budget \
      --field max_total_tokens < "$bodyfile" 2>/dev/null) || ceiling=""
  fi
  printf '  issue     %s tokens across every stage of #%s' \
    "$(issue_total_tokens "$issue")" "$issue"
  [ -n "$ceiling" ] && printf '  (ceiling %s)' "$ceiling"
  printf '\n'
}

# ----------------------------------------------------------------------------------------------
# STAGES (#375). What the issue plans and what the branch has actually done, resolved together
# into these globals because every caller wants all of them and a bash function returns one
# string. Progress is read off the commits, never off anything an agent wrote down
# (agent_os/docs/adr/2026-09-14-driver-writes-mechanical-state-agent-writes-cooperative-state.md).
# ----------------------------------------------------------------------------------------------
stage_issue_body=""   # the issue's own body, the source of both the plan and the prompt
stage_base_ref=""     # the ref HEAD forked from -- `Base: <branch>` if the issue names one
stage_fork_point=""   # merge-base of that ref and HEAD: where this issue's own commits start
stages_done=0         # highest N among the `stage N/M:` commits since the fork point
stages_total=0        # how many `- [ ]` lines the issue's `## Stages` checklist carries

resolve_stage_context() {
  local issue=$1 base candidate
  stage_issue_body=""; stage_base_ref=""; stage_fork_point=""; stages_done=0; stages_total=0
  case "$issue" in ''|*[!0-9]*) return 1 ;; esac
  stage_issue_body=$(gh issue view "$issue" --json body -q .body 2>/dev/null) || return 1
  printf '%s\n' "$stage_issue_body" > "$bodyfile"
  stages_total=$("$agent_python" -m agent_os.lib stage-titles "$bodyfile" | grep -c .)

  # Same `Base: <branch>` resolution `open-pr` and `start` use, and the SAME ORDER as `open-pr`:
  # fetched, `origin/<base>` first. A local base branch that has not moved in days puts the fork
  # point far behind, and everything origin has merged since then falls inside this issue's stage
  # range -- which matters now that `open-pr` merges the base INTO the branch before pushing
  # (#389): the range would fill with other issues' `stage N/M:` commits, and the count below is
  # a maximum with no issue filter, so resume would skip a stage or call the issue done (#389
  # review). The fetch is best-effort: offline, an earlier `origin/<base>` still beats the local
  # one.
  base=$(issue_base_branch "$stage_issue_body")
  git -C "$worktree" fetch -q origin "$base" 2>/dev/null || true
  for candidate in "origin/$base" "$base"; do
    git -C "$worktree" rev-parse --verify -q "${candidate}^{commit}" >/dev/null 2>&1 || continue
    stage_base_ref=$candidate
    break
  done
  [ -n "$stage_base_ref" ] || return 0
  stage_fork_point=$(git -C "$worktree" merge-base "$stage_base_ref" HEAD 2>/dev/null || true)
  [ -n "$stage_fork_point" ] || return 0
  # `--first-parent --no-merges`: THIS BRANCH'S OWN commits and nothing else. A base merged into
  # the branch arrives as a merge commit's second parent, so without this the base's history --
  # every other issue's stage commits included -- counts as this issue's progress.
  stages_done=$(git -C "$worktree" log --first-parent --no-merges --format=%s "$stage_fork_point..HEAD" \
    | "$agent_python" -m agent_os.lib stages-completed)
}

# `Base: <branch>` on a line of its own, and `main` when the issue names none. ONE resolution of
# "which branch does this issue's work belong on", read by `resolve_stage_context` (where the
# issue's own commits start), by `open-pr` (what the pull request is diffed against) and by
# `start`'s base-branch refusal (#388) -- the same sed had been written out three times, and a
# gate that disagreed with `open-pr` about the base would refuse the dispatches it should allow.
issue_base_branch() {
  local base
  base=$(printf '%s\n' "$1" \
    | sed -n 's/^Base:[[:space:]]*\([^[:space:]][^[:space:]]*\)[[:space:]]*$/\1/p' | head -1)
  printf '%s\n' "${base:-main}"
}

# The title of one stage, by its 1-based number, straight from the checklist -- the string the
# worker's commit subject has to match.
stage_title() { "$agent_python" -m agent_os.lib stage-titles "$bodyfile" | sed -n "$1p"; }

# Is this commit subject a guard cut? The pre-merge freeze has the same shape and is not one
# (#407): it says the run reached `open-pr` with every stage committed, which is the opposite of
# unfinished work. Everything that reads a freeze subject comes through here, so the relaunch cap
# and the relaunched stage's prompt cannot disagree about what a cut is.
subject_is_a_guard_cut() {
  case "$1" in
    "$PRE_MERGE_FREEZE_SUBJECT") return 1 ;;
    "$WIP_SUBJECT"*) return 0 ;;
  esac
  return 1
}

# How many of the subjects on stdin are guard cuts. A loop rather than `grep -c` because the
# pre-merge freeze is an excluded subject and not a pattern, and because a branch with no cut at
# all has to come back as a count of zero rather than as a failed command.
count_guard_cut_subjects() {
  local subject cuts=0
  while IFS= read -r subject; do
    subject_is_a_guard_cut "$subject" && cuts=$((cuts + 1))
  done
  printf '%d\n' "$cuts"
}

# Is the newest commit on this branch a cut freeze rather than a finished stage? Newest first,
# whichever comes first wins: a `WIP: cut by guard` commit above the last `stage N/M:` one is
# unfinished work on the stage about to be relaunched, and the worker must build on it. The
# pre-merge freeze is walked straight through (#407) -- it is not unfinished work, and the stage
# commit below it is what says how far the branch got.
has_wip_after_last_stage_commit() {
  local subject
  [ -n "$stage_fork_point" ] || return 1
  while IFS= read -r subject; do
    case "$subject" in
      "stage "[0-9]*) return 1 ;;
    esac
    subject_is_a_guard_cut "$subject" && return 0
  done <<< "$(git -C "$worktree" log --first-parent --no-merges --format=%s "$stage_fork_point..HEAD")"
  return 1
}

# The stage process that just ended, filed where the whole issue's spend can be summed. Archiving
# always empties the live file, which is what makes double-archiving impossible: an empty live
# file has nothing left to file.
archive_stage_events() {
  local issue=$1 stage_number=$2
  [ -s "$events" ] || return 0
  case "$issue" in ''|*[!0-9]*) return 0 ;; esac
  mkdir -p "$spenddir/$issue"
  cp "$events" "$spenddir/$issue/$(date -u +%Y%m%dT%H%M%SZ)-$backend-stage$stage_number.jsonl"
  : > "$events"
}

# The files one issue's spend is summed over: every stage log archived so far plus the live one,
# which `archive_stage_events` files when its stage process ends. ONE listing for both sums, so
# the dollar ceiling and the token ceiling can never be measured over a different set of stages
# (#387, the same rule `agent_guard.issue_stage_logs` states for the guard's own tick).
issue_spend_logs() {
  local issue=$1 path
  for path in "$spenddir/$issue"/*.jsonl; do [ -f "$path" ] && printf '%s\n' "$path"; done
  [ -s "$events" ] && printf '%s\n' "$events"
  return 0
}

# `max_cost_usd` is the ISSUE's ceiling, not this process's: every stage it has taken counts
# towards it (#375). The archived logs plus whatever the live one has collected since.
issue_cost_usd() {
  local issue=$1 archived=()
  mapfile -t archived < <(issue_spend_logs "$issue")
  [ "${#archived[@]}" -gt 0 ] || { echo 0; return 0; }
  "$agent_python" -m agent_os.lib cumulative-cost "${archived[@]}"
}

# `max_total_tokens` is the issue's other ceiling, and on a worker class the only one that can
# ever fire: Qwen's terminal `result` event carries `usage.total_tokens` and no `total_cost_usd`,
# so the dollar sum above reads 0.0 for every stage of it (#387). Same files, and the same
# undercount for a stage cut before its terminal event -- `agent_lib.cumulative_total_tokens`
# states that rule rather than papering over it.
issue_total_tokens() {
  local issue=$1 archived=()
  mapfile -t archived < <(issue_spend_logs "$issue")
  [ "${#archived[@]}" -gt 0 ] || { echo 0; return 0; }
  "$agent_python" -m agent_os.lib cumulative-tokens "${archived[@]}"
}

# One whole-issue ceiling of this issue's budget class, read off the body its stages were planned
# from. Empty when the class declares none, and an empty ceiling passes nothing: the gate cuts on
# a measurement, never on the absence of one.
class_ceiling() {
  printf '%s\n' "$stage_issue_body" \
    | "$agent_python" -m agent_os.lib resolve-budget --field "$1" 2>/dev/null
}

# `spent >= cap` on two strings, one of them a float: awk reads both, and bash arithmetic reads
# neither. `awk` rather than a python call because the gate runs this twice per stage exit.
ceiling_passed() {
  [ -n "$1" ] && [ -n "$2" ] \
    && awk -v spent="$1" -v cap="$2" 'BEGIN { exit !(spent + 0 >= cap + 0) }'
}

# The repo-wide human-only full stop, read the same way the guard's tick reads it: the tracking
# epic carrying the paused label. A `gh` failure reads as "not paused" rather than stopping the
# chain forever on a transient API hiccup, exactly as in agent_guard._agents_paused.
agents_paused() {
  local labels
  labels=$(gh issue view "$(project_value tracking_epic)" --json labels -q '.labels[].name' 2>/dev/null) || return 1
  printf '%s\n' "$labels" | grep -qx "$(project_value labels.agents_paused)"
}

# Freeze whatever the process left behind so the next process starts from a committed tree.
# Returns 0 if something was actually frozen. This is the ONE freeze, and every path that ends a
# run reaches it: the four callers below, and the guard's `cut_run` through the `freeze`
# subcommand (#482). The guard used to keep a `git add -u` of its own here, so a run a tick cut was
# frozen without the untracked half #417 added -- #457's cut carried one modified file and left the
# two new ones, some 900 lines, untracked behind their own freeze commit.
# The diary is the one path this never stages, whatever the reason (#407): a freeze that swept it
# in published it inside a `WIP: cut by guard` commit, and its uncommitted lines are the dirty
# signal `start`/`resume`/`branch` read. The lines stay in the worktree either way.
freeze_uncommitted_work() {
  local reason=$1 untracked_pathspec=(. ":!$DIARY") swept=() swept_note="" path
  git -C "$worktree" add -u -- . ":!$DIARY"
  # The exclusion above governs what THIS call adds, not what is already in the index: a worker
  # that staged the diary by hand before it was cut put it there itself. `reset -- <path>` moves
  # the index entry and nothing else, so it is not the destructive reset the RULES forbid.
  git -C "$worktree" reset -q HEAD -- "$DIARY" 2>/dev/null || true
  # A FILE THE STAGE NEVER ADDED IS THE STAGE'S WORK TOO (#417). `git add -u` reaches only paths
  # git already tracks, so a cut stage that had written a new file left it untracked -- dirty
  # behind its own freeze, which is the one state `resume` refuses: #416's stage 2 wrote two new
  # files, some 1,200 lines, the freeze took the four tracked ones, and the task then waited for a
  # human to commit by hand what the mechanism had just produced. Still never `git add -A`, which
  # has destroyed a symlink in this repo: the paths arrive one by one from `ls-files --others
  # --exclude-standard`, so what `.gitignore` or `.git/info/exclude` keeps out stays out, and the
  # listing is also what the commit body below names. `.env` is the link this driver creates
  # itself (#404), dropped only while it holds a link -- the same narrowing `uncommitted_work`
  # reads, so the freeze cannot leave behind the one entry that refusal was taught to ignore.
  [ -L "$worktree/.env" ] && untracked_pathspec+=(":!.env")
  while IFS= read -r -d '' path; do swept+=("$path"); done \
    < <(git -C "$worktree" ls-files --others --exclude-standard -z -- "${untracked_pathspec[@]}")
  if [ "${#swept[@]}" -gt 0 ]; then
    git -C "$worktree" add -- "${swept[@]}"
    swept_note="The freeze also added these files, which the stage left untracked:"
    swept_note+=$'\n'"$(printf '  %s\n' "${swept[@]}")"
  fi
  git -C "$worktree" diff --cached --quiet && return 1
  if [ -n "$swept_note" ]; then
    # Named in the body, not only taken: a reader of the branch has to be able to tell work the
    # agent committed from work a freeze swept in.
    git -C "$worktree" commit -q -m "$WIP_SUBJECT ($reason)" -m "$swept_note"
  else
    git -C "$worktree" commit -q -m "$WIP_SUBJECT ($reason)"
  fi
}

# Uncommitted work in the worktree, at most the five entries a refusal prints -- WITHOUT the `.env`
# link this driver puts there itself (#404). `launch_stage` links `$main/.env` into a worktree that
# has none, and the dirty check below read that link as untracked work: a relaunch was refused for
# the driver's own doing, and in a repository carrying no ignore rule for `.env` -- a test's
# temporary one, or any project that does not gitignore it -- it was refused every time. This
# repository does ignore it, which is why the real worktrees never showed the defect; the guarantee
# cannot rest on that, because the driver is project-agnostic and knows no `.gitignore` of its own.
uncommitted_work() {
  local entries
  entries=$(git -C "$worktree" status --porcelain)
  # Narrow on purpose: only that path's UNTRACKED entry, and only while the path holds a symlink,
  # which is the shape the driver's link has. A `.env` that is a real file, a tracked `.env` git
  # reports as modified or typechanged, the diary and every other path still count as work.
  # Filtered before the five entries are taken, so the link cannot crowd a real one out.
  if [ -L "$worktree/.env" ]; then
    entries=$(printf '%s\n' "$entries" | grep -v -x '?? \.env' || true)
  fi
  [ -n "$entries" ] || return 0
  printf '%s\n' "$entries" | head -5
}

# What happened to work the process never committed -- the last clause of the progress comment.
# The same reading as the refusals below: a link the driver created is not work the worker left.
stage_tree_state() {
  [ -n "$(uncommitted_work)" ] && { echo "uncommitted work left behind"; return; }
  echo "tree clean"
}

# One comment per run end, under the worker's own App identity, saying what the planner and the
# human both need at a glance: how far the staged work got and whether anything is uncommitted.
# TODO(#366): render from project.messages
post_stage_progress_comment() {
  local issue=$1 frozen=$2 branch last
  case "$issue" in ''|*[!0-9]*) return 0 ;; esac
  branch=$(git -C "$worktree" rev-parse --abbrev-ref HEAD)
  last=$(git -C "$worktree" log --format='%h %s' -1)
  "$agent_python" -m agent_os.issues update "$issue" --comment \
    "Stages $stages_done/$stages_total done on $branch; last commit $last; $frozen" \
    || echo "WARNING: could not comment the stage progress on #$issue"
}

# THE LABEL FOLLOWS THE RUN, NOT WHOEVER SET IT LAST (#385). An issue a worker is running always
# carries `status:doing`. `start` set it and nothing else did: a relaunch and every chained stage
# inherited whatever label the issue happened to hold, and a human's reply clearing
# `status:blocked-on-human` under a live worker left the board showing NOTHING for work that was
# actually happening -- seen twice on #363 on 2026-09-16 and put back by hand both times. It
# self-heals only when `open-pr` moves the issue to `ai-completed`, which never happens if the run
# is cut.
#
# READ FIRST, AND ONLY FILL A VACUUM. Re-asserting `doing` unconditionally reverted a human who
# had labelled a LIVE issue `blocked-on-human` (taking its question over) or `review` -- at the
# next stage boundary, silently. It also cost eight `gh` round-trips and a one-second sleep per
# boundary, some eighty on a ten-stage run, to write a label that was already right. ONE read
# decides: any `status:*` label at all is somebody's decision and is left alone, and only an issue
# carrying none is put back on `doing`, which is the case this exists for. A read that FAILS
# changes nothing either -- acting on a `gh` hiccup is how the label got reverted to begin with.
ensure_doing_label() {
  local issue=$1 labels held
  case "$issue" in ''|*[!0-9]*) return 0 ;; esac
  labels=$(gh issue view "$issue" --json labels -q '.labels[].name' 2>/dev/null) || {
    echo "WARNING: could not read #$issue's labels; leaving whatever it carries alone"
    return 0
  }
  held=$(printf '%s\n' "$labels" | grep '^status:' || true)
  if [ -n "$held" ]; then
    echo "#$issue already carries $(printf '%s' "$held" | tr '\n' ' ')-- left as it is"
    return 0
  fi
  if "$agent_python" -m agent_os.issues move "$issue" doing; then
    write_state_marker "$issue" "$(project_value labels.doing)"
  else
    echo "WARNING: could not keep #$issue on doing; the run continues anyway"
  fi
}

# ONE STAGE, ONE PROCESS. `start`, `resume` and the exit hook's chaining all land here: the same
# launch, differing only in what each of them checked before getting here. Always the last act of
# the process that calls it, so it exits rather than returning. Never `--resume` of the backend's
# own session: a stage boundary is a green, committed tree, so nothing in the previous process's
# context is worth carrying, which is the whole point of staging (#375).
launch_stage() {
  local mode=$1 issue=$2 brief=$3 after=$4 extra_context=$5
  local next_stage stage_goal stages_summary recent_commits launched_state_line

  if [ -z "$model" ]; then
    echo "no model to launch backend '$backend' with: set WORKER_MODEL, or give a class in config/agents.yaml backend: $backend"
    return 1
  fi

  # A worktree without `.env` is a worker whose SEC and Tiingo tools refuse before they reach the
  # network, and the error they raise -- "Falta la variable de entorno SEC_USER_AGENT" -- surfaces
  # to an agent as a bare `Error executing tool`. In CP 6.1's first batch five subagents read that
  # as "the MCP server has no network", went around it with curl, and the run had to re-verify
  # every citation it had already gathered. `.env` is gitignored, so `git worktree add` never
  # brings it, and a copy drifts from the original the day a token is rotated: the link is what
  # keeps one file authoritative for every tree. What this driver creates here it does not then
  # count as work the worker left behind: `uncommitted_work` drops the link, so a relaunch is
  # never refused -- nor reported as uncommitted -- over the previous launch's own doing (#404).
  if [ ! -e "$worktree/.env" ] && [ -f "$main/.env" ]; then
    ln -s "$main/.env" "$worktree/.env"
    echo "linked $worktree/.env -> $main/.env (it was missing)"
  fi

  # GitHub identity: each backend is its own GitHub App, so its
  # comments, PRs and commits are attributable to it and never to the human account. Secrets are
  # per-app JSON+PEM under .secrets/ (gitignored); missing them degrades to the old anonymous
  # behavior with one warning rather than failing the run -- see docs/modules/workers.md#identities.
  if agent_apply_identity "$(backend_value "$backend" app)"; then
    # Plain `git push` (as opposed to `gh`) needs its own credential lookup; this makes it read
    # GH_TOKEN from the environment instead of asking. Worktree-local, never global.
    git -C "$worktree" config credential.helper '!gh auth git-credential'
  fi

  # An issue with no `## Stages` section resolves to zero stages and is launched exactly as it was
  # before #375: the gate refuses to dispatch one, so the only way here is a human's `--force`,
  # and refusing to launch it at all would be a worse answer than launching it unstaged.
  resolve_stage_context "$issue" || true
  if [ "$stages_total" -gt 0 ] && [ "$stages_done" -ge "$stages_total" ]; then
    # A refusal, not a finish: nothing ran, so this must not write `worker_finished` -- that event
    # reads exactly like the genuine completion `agent_os.guard:888` writes and woke the
    # planner for free, for work that never happened (#443). `write_state DONE` and exit 0 are
    # untouched: the issue really has nothing left to launch, and the planner reads that off the
    # state file (or off a genuine `worker_finished`/`worker_cut` from an earlier stage) whenever
    # it next looks, without needing an event to tell it to look.
    echo "all $stages_total stage(s) of #$issue are already committed -- nothing to launch"
    write_state DONE
    exit 0
  fi

  # `start` has already moved the issue to `doing` (and refused the dispatch if it could not);
  # a `resume` and every chained stage assert it again here, so live work is never unlabelled
  # (#385). Below the "all stages committed" exit above on purpose: that path ends the run and
  # must not relabel an issue whose work is finished.
  case "$mode" in resume|chain) ensure_doing_label "$issue" ;; esac

  # Whatever the previous process spent is filed before the live log is truncated below: it counts
  # towards this issue's `max_cost_usd` even when the exit hook never got to archive it itself (a
  # guard cut kills the whole process group, this call is what catches up).
  archive_stage_events "$issue" "$stages_done"

  [ "$mode" = start ] && git -C "$worktree" rev-parse HEAD > "$startref"
  [ -s "$startref" ] || git -C "$worktree" rev-parse HEAD > "$startref"
  # A fresh context per stage is the point: `max_context` is judged per process, so each one gets
  # its own event stream.
  : > "$events"
  : > "$logfile"
  launched_state_line=STARTED
  [ "$mode" = resume ] && launched_state_line="RESUMED after=$after"
  write_state "$launched_state_line"
  printf '%s/%s\n' "$stages_done" "$stages_total" > "$stagefile"

  echo "brief:     $brief"
  echo "worktree:  $worktree ($(git -C "$worktree" rev-parse --abbrev-ref HEAD) @ $(git -C "$worktree" rev-parse --short HEAD))"
  echo "model:     $model"
  [ "$stages_total" -gt 0 ] && echo "stage:     $((stages_done + 1))/$stages_total of issue #$issue"

  # Project-specific environment for the worker's own backend process -- a common example is a
  # read-only database URL, but the mechanism itself names nothing (agent_os/docs/adr/2026-09-14-the-agent-mechanism-
  # is-project-agnostic-and-configured-not-coded.md, agent_os/docs/adr/2026-09-15-workers-connect-read-
  # only-by-default-and-reach-the-owner-only-through-the-test-runner.md). Exported here, into this
  # shell, so the backend CLI launched below inherits it.
  while IFS=$'\t' read -r env_key env_value; do
    [ -n "$env_key" ] && export "$env_key=$env_value"
  done < <("$agent_python" -m agent_os.lib worker-environment)

  export PYTHONPATH="$worktree"
  export WORKER_RULES="$RULES" WORKER_BRIEF="$brief" WORKER_MODEL_ID="$model"
  # AGENTS.md, then the brief (the issue and its parent), then only what those name -- in that
  # order and nothing else. agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-
  # are-the-mechanical-state.md
  export WORKER_FIRST_INSTRUCTION="Read AGENTS.md first. Then read your brief at $brief: it is the
GitHub issue you are working on and its parent issue, which together are the whole task. Then read
only the docs, ADRs and paths the brief itself names -- nothing else. Then carry it out in full."
  [ -n "$extra_context" ] && export WORKER_FIRST_INSTRUCTION="$WORKER_FIRST_INSTRUCTION

Since your last turn, this arrived and is part of the task now:
$extra_context"

  # The whole issue is the brief; ONE stage of it is this process's goal. What is already done is
  # named from the commits, not from a status an agent claimed, and the three last subjects are
  # there so the worker can see the shape of what it is continuing.
  if [ "$stages_total" -gt 0 ]; then
    next_stage=$((stages_done + 1))
    stage_goal=$(stage_title "$next_stage")
    stages_summary=none
    [ "$stages_done" -gt 0 ] && stages_summary="1..$stages_done"
    recent_commits=$(git -C "$worktree" log --format='  %h %s' -3)
    WORKER_FIRST_INSTRUCTION="$WORKER_FIRST_INSTRUCTION

THIS PROCESS IS ONE STAGE OF THAT ISSUE, NOT THE WHOLE ISSUE.
Stages already done: $stages_summary of $stages_total (read off this branch's own commits).
Your only goal in this process: stage $next_stage/$stages_total: $stage_goal
The last three commits on this branch:
$recent_commits"
    if has_wip_after_last_stage_commit; then
      WORKER_FIRST_INSTRUCTION="$WORKER_FIRST_INSTRUCTION

The newest commit is a 'WIP: cut by guard' freeze of unfinished work on this same stage: build on
it, do not start the stage over."
    fi
    export WORKER_FIRST_INSTRUCTION="$WORKER_FIRST_INSTRUCTION

Close the stage with a commit whose subject is exactly 'stage $next_stage/$stages_total: $stage_goal'
and then stop. The driver launches the next stage in a new process."
  fi

  export WORKER_EVENTS="$events" WORKER_MAIN="$main" WORKER_BACKEND_BIN="$backend_bin"
  export WORKER_BACKEND="$backend"
  # setsid so the run outlives this shell and can be stopped as one process group. The rules go
  # through the environment rather than the command line, so no quoting can mangle them.
  #
  # No `exec` on the backend CLI below: this subshell must still be alive after it exits, to call
  # the driver's own end-of-stage decision and, when the run really ends there, the guard's exit
  # hook in the same process (agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-never-
  # as-a-standing-process.md -- "a hook fires on every worker's natural end"). A tick that cuts
  # this same run kills the whole process group (-TERM -pgid reaches this subshell too, not just
  # the CLI child), so the hook only ever runs on a natural exit, and `check` itself never
  # overwrites a CUT_BY_GUARD a tick already wrote.
  # The dialect is the backend's `stream:` capability, not its name (#514): the flags that make a
  # CLI write a stream-json log belong to the CLI whose shape that log is.
  case "$backend_stream" in
  qwen_jsonl)
    setsid nohup bash -c '
        cd "$1" || exit 1
        "$WORKER_BACKEND_BIN" --model "$WORKER_MODEL_ID" --approval-mode yolo -o stream-json \
             --append-system-prompt "$WORKER_RULES" \
             "$WORKER_FIRST_INSTRUCTION" \
             >>"$WORKER_EVENTS"
        backend_status=$?
        # The end of ONE STAGE, which is only sometimes the end of the run: when another stage
        # remains and the gates pass, `stage-exit` has already launched it in a process group of
        # its own and this one must stop here without publishing anything.
        WORKER_WORKTREE="$1" WORKER_BACKEND_STATUS=$backend_status \
          "$AGENT_OS_DIR/bin/worker_task.sh" "$WORKER_BACKEND" stage-exit && exit 0
        WORKER_WORKTREE="$1" "$AGENT_OS_DIR/bin/worker_task.sh" "$WORKER_BACKEND" open-pr
        "$AGENT_OS_PYTHON" -m agent_os.guard check "$WORKER_BACKEND"
      ' _ "$worktree" >>"$logfile" 2>&1 &
    ;;
  claude_jsonl)
    # --add-dir lets the worker READ the main checkout (the rules allow reading `.cache/`); the
    # collect step checks the main tree is untouched because a write there would be invisible to
    # the branch diff. --dangerously-skip-permissions is what "headless" means; the rules and the
    # audit are the guardrails, exactly as for the Qwen backend's yolo mode.
    setsid nohup bash -c '
        cd "$1" || exit 1
        "$WORKER_BACKEND_BIN" -p --model "$WORKER_MODEL_ID" --output-format stream-json --verbose \
             --dangerously-skip-permissions --add-dir "$WORKER_MAIN" \
             --append-system-prompt "$WORKER_RULES" \
             "$WORKER_FIRST_INSTRUCTION" \
             >>"$WORKER_EVENTS"
        backend_status=$?
        WORKER_WORKTREE="$1" WORKER_BACKEND_STATUS=$backend_status \
          "$AGENT_OS_DIR/bin/worker_task.sh" "$WORKER_BACKEND" stage-exit && exit 0
        WORKER_WORKTREE="$1" "$AGENT_OS_DIR/bin/worker_task.sh" "$WORKER_BACKEND" open-pr
        "$AGENT_OS_PYTHON" -m agent_os.guard check "$WORKER_BACKEND"
      ' _ "$worktree" >>"$logfile" 2>&1 &
    ;;
  esac
  echo $! > "$pidfile"
  sleep 2
  # This check cannot tell a failed launch from a working run, and deliberately does not try
  # (#381): what it asks is "is the backgrounded SUBSHELL alive", and that subshell outlives the
  # backend CLI by design -- it still has `stage-exit`, `open-pr` and the guard's exit hook to run,
  # and reaching `stage-exit` alone costs seconds of driver start-up. A launch that never happened
  # is recorded by `stage-exit` moments later, as `FAILED_LAUNCH`, and reaches the planner as the
  # `worker_cut` event the exit hook writes -- the same way every other terminal state does.
  if alive; then
    echo "started pid $(cat "$pidfile")"
    echo "events $events   log $logfile"
  elif [ "$(head -n1 "$statefile" 2>/dev/null || true)" != "$launched_state_line" ]; then
    # A run shorter than this two-second settling check is not a failed launch: the state line
    # this function wrote a moment ago has already been replaced by a terminal one, which only
    # the end of the run writes (`stage-exit`'s cut, or the guard's exit hook). Reporting it as a
    # failure would tell the planner the dispatch never happened while the run's own events say
    # it did -- and a one-turn stage, or a backend refused at once, ends exactly this fast.
    echo "ran and ended before the start check: $(head -n1 "$statefile")"
    echo "events $events   log $logfile"
  else
    echo "failed to start; log follows"; tail -10 "$logfile"; exit 1
  fi
  exit 0
}

case "${1:-status}" in

rules)
  # The resolved block, for reading and for a test -- no worktree touched, no backend called.
  echo "$RULES"
  ;;

init)
  # Idempotent: a worktree already there (however it got there -- by hand, or a previous `init`)
  # is left exactly alone, on whatever branch it is already on. This is the ONLY subcommand that
  # may run before the worktree exists at all (agent_os/docs/AGENT_OS.md §7 row (r), issue #392): every
  # other one refuses on `[ -e "$worktree/.git" ]` the same way `branch` does above.
  if [ -e "$worktree/.git" ]; then
    echo "$worktree already initialized (on $(git -C "$worktree" branch --show-current 2>/dev/null || echo '?'))"
    exit 0
  fi
  # Same base resolution as `branch`'s own no-`<from>` case: the remote's tip, fetched from the
  # MAIN checkout (there is no worktree yet to fetch from). A fresh branch, never `main` itself --
  # a worktree cannot check out a branch another worktree (this one) already has checked out.
  git -C "$main" fetch -q origin main \
    || { echo "could not fetch origin/main -- refusing to init a worktree from a base nobody can name"; exit 1; }
  init_branch="agent-os/init-$backend"
  git -C "$main" worktree add -q -b "$init_branch" "$worktree" origin/main \
    || { echo "git worktree add failed for $worktree"; exit 1; }
  echo "created $worktree on $init_branch @ $(git -C "$worktree" rev-parse --short HEAD) (from origin/main)"
  # `git worktree add` brings tracked files only; without these two gitignored links a worker
  # cannot run a test (`.venv`) or reach a network credential (`.env`) the first time it starts --
  # the same reasoning `agent_prepare_worktree`'s throwaway worktree already applies to a role run.
  # A link, never a copy: one file stays authoritative for every tree.
  for linked in .venv .env; do
    if [ ! -e "$worktree/$linked" ] && [ -e "$main/$linked" ]; then
      ln -s "$main/$linked" "$worktree/$linked"
      echo "linked $worktree/$linked -> $main/$linked"
    fi
  done
  ;;

branch)
  name=${2:-}; from=${3:-}
  [ -n "$name" ] || { echo "usage: $0 $backend branch <name> [<from>]"; exit 2; }
  alive && { echo "a run is alive (pid $(cat "$pidfile")); stop it before switching branches"; exit 1; }
  [ -e "$worktree/.git" ] || { echo "no worktree at $worktree"; exit 1; }
  dirty=$(uncommitted_work)
  [ -n "$dirty" ] && { echo "worktree is dirty; commit or clean it first:"; echo "$dirty"; exit 1; }
  # No explicit base: start from the remote's tip, never from whatever the shared `.git` happens
  # to hold for the local `main` -- that ref only moves when a human's own checkout runs `pull`,
  # so a merge nobody pulled left a dispatch building on a base already behind `origin/main` (#435).
  # The fetch is not best-effort here: a base nobody can name is worse than no dispatch, so a
  # failure (offline, no credentials) refuses rather than silently falling back to the stale ref.
  # An explicit `<from>` is honoured verbatim and skips this entirely -- resuming on a named
  # commit or an existing branch is exactly what that argument is for.
  if [ -z "$from" ]; then
    git -C "$worktree" fetch -q origin main \
      || { echo "could not fetch origin/main -- refusing to branch from a base nobody can name"; exit 1; }
    from=origin/main
  fi
  git -C "$worktree" checkout -q -B "$name" "$from" || exit 1
  echo "$worktree is now on $name @ $(git -C "$worktree" rev-parse --short HEAD) (from $from)"
  ;;

start|resume)
  mode=$1; shift
  force=no; after=manual; extra_context=""; positional=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=yes ;;
      --after) shift; after=${1:-manual} ;;
      # What the planner hands a resumed run that has something new to act on -- today, the body
      # of the validator's request-changes review (agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-
      # validator-agent-against-the-issues-acceptance-criteria.md). It is appended to the fixed
      # first instruction, never substituted for the brief: the issue stays the task.
      --context) shift; extra_context=${1:-} ;;
      *) positional+=("$1") ;;
    esac
    shift
  done

  alive && { echo "a run is already alive (pid $(cat "$pidfile")); stop it first"; exit 1; }
  [ -e "$worktree/.git" ] || { echo "no worktree at $worktree"; exit 1; }
  dirty=$(uncommitted_work)
  [ -n "$dirty" ] && { echo "worktree is dirty; commit or clean it first:"; echo "$dirty"; exit 1; }

  if [ "$mode" = start ]; then
    issue=${positional[0]:-}
    extra=${positional[1]:-}
    case "$issue" in ''|*[!0-9]*)
      echo "usage: $0 $backend start <issue> [extra-brief.md] [--force]  -- the issue IS the brief"; exit 2 ;;
    esac
    [ -z "$extra" ] || [ -f "$extra" ] || { echo "no such supplement: $extra"; exit 1; }

    # THE WORKTREE MUST BE WHERE THIS ISSUE'S WORK BELONGS (#388). A finished dispatch leaves the
    # worktree on its own branch and nothing used to look: after #363 finished, the Qwen worktree
    # stayed on `task/363-prompts-config-literals`, and the next dispatch would have written its
    # commits inside that already-open pull request. Accepted are the issue's base branch and any
    # branch whose name carries this issue's number -- the shape the planner's own `branch
    # task/<N>-<slug>` produces. Same precedent as the parallelism cap: a rule the planner is told
    # to follow becomes a rule the driver enforces (agent_os/docs/adr/2026-09-15-parallelism-is-a-
    # configured-cap-enforced-by-the-driver.md). First of the gates because it writes nothing and,
    # on the normal path -- the branch the planner has just created for this issue -- it is a
    # string comparison that costs no `gh` call, while the two gates after it do.
    # `branch --show-current` rather than `rev-parse --abbrev-ref HEAD`: the latter answers the
    # literal `HEAD` (and fails) on a branch with no commits yet, which is a perfectly ordinary
    # state for a freshly created worktree. Empty here means a detached HEAD, which is on no
    # branch at all and so on neither of the two this accepts.
    # THE IDENTITY BEFORE THE FIRST `gh` CALL, not after it -- #363's lesson, applied to `start`'s
    # own gates: the ambient token this shell inherited may be an hour old and expired, and a read
    # that answers 401 must not be the thing that decides where this dispatch may write.
    if agent_apply_identity "$(backend_value "$backend" app)"; then
      git -C "$worktree" config credential.helper '!gh auth git-credential'
    fi

    # ONE read of the body, for the base gate here and the budget class below, and it HARD-FAILS.
    # A `|| true` here resolved a failed read to an empty body, an empty body resolves the base to
    # `main`, and the gate then refused a correctly-placed stacked dispatch and ACCEPTED a
    # worktree wrongly left on `main` -- wrong in both directions, from the same swallowed error.
    body=$(gh issue view "$issue" --json body -q .body) \
      || { echo "refusing to dispatch: could not read issue #$issue"; exit 1; }

    current_branch=$(git -C "$worktree" branch --show-current)
    if [ "$force" = yes ]; then
      echo "base-branch check skipped (--force): $worktree is on ${current_branch:-a detached HEAD}"
    # ANCHORED ON THE PLANNER'S OWN SHAPE, `<word>/<issue>-<slug>`. An unanchored token search
    # accepted `task/387-close-the-390-gap` as a branch for #390 and `chore/2026-09-16-cleanup`
    # for #16 -- and accepting a wrong branch is the whole failure this gate exists to stop.
    elif ! printf '%s\n' "$current_branch" | grep -qE "(^|/)[a-z]+/$issue([-/]|$)"; then
      issue_base=$(issue_base_branch "$body")
      if [ "$current_branch" != "$issue_base" ]; then
        echo "refusing to dispatch: $worktree is on ${current_branch:-a detached HEAD}, but issue" \
          "#$issue's base is $issue_base -- put the worktree on it" \
          "($0 $backend branch task/$issue-<slug> $issue_base) or on a branch naming #$issue," \
          "or pass --force"
        exit 1
      fi
    fi

    # PARALLELISM CAP AND MODULE EXCLUSION, ENFORCED HERE, NOT COUNTED BY THE PLANNER (#374,
    # agent_os/docs/adr/2026-09-15-parallelism-is-a-configured-cap-enforced-by-the-driver.md). This backend
    # is not alive or `start` would already have refused above -- so every alive backend found
    # here is an OTHER one, and the count below is exactly how many issues are already running.
    other_backends_alive=()
    for other in $("$agent_python" -m agent_os.lib worktree-backends); do
      [ "$other" = "$backend" ] && continue
      alive_pidfile "$cache/worker_$other.pid" && other_backends_alive+=("$other")
    done
    max_parallel_issues=$("$agent_python" -m agent_os.lib planner-value max_parallel_issues)
    if [ "${#other_backends_alive[@]}" -ge "$max_parallel_issues" ]; then
      echo "refusing to dispatch: ${#other_backends_alive[@]} worker(s) already running" \
        "(>= planner.max_parallel_issues=$max_parallel_issues) -- wait for one to finish"
      exit 1
    fi
    # Below the cap on count alone, two running issues must still never share a `module:` label:
    # a worker editing one module's code and its docs/modules/*.md produces a conflict the other
    # worker cannot see. Compared against every OTHER backend that is actually alive, never
    # against this one's own past run.
    issue_modules=$(gh issue view "$issue" --json labels -q '.labels[].name' 2>/dev/null \
      | grep '^module:' || true)
    for other in "${other_backends_alive[@]}"; do
      other_issuefile=$cache/worker_$other.issue
      [ -s "$other_issuefile" ] || continue
      other_issue=$(cat "$other_issuefile")
      case "$other_issue" in ''|*[!0-9]*) continue ;; esac
      other_modules=$(gh issue view "$other_issue" --json labels -q '.labels[].name' 2>/dev/null \
        | grep '^module:' || true)
      shared=$(comm -12 <(printf '%s\n' "$issue_modules" | sort) <(printf '%s\n' "$other_modules" | sort) \
        | grep . || true)
      if [ -n "$shared" ]; then
        echo "refusing to dispatch: issue #$issue shares module label(s) [$(echo "$shared" | tr '\n' ' ')]" \
          "with issue #$other_issue already running on $other"
        exit 1
      fi
    done

    # The issue is the unit of work: a body that does not validate is not a brief, and dispatching
    # it anyway is how a worker ends up inventing its own task. --force is for the human who knows
    # why. agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    # state.md
    if [ "$force" = no ]; then
      "$agent_python" -m agent_os.issues validate "$issue" \
        || { echo "refusing to dispatch: issue #$issue does not validate (pass --force to override)"; exit 1; }
    fi

    # An issue is dispatchable only once its body resolves to a task class in config/agents.yaml --
    # a dispatch with no budget is a bug in the queue, never a judgment call for whoever is
    # dispatching. agent_os/docs/adr/2026-09-14-agent-spend-is-tokens-not-time-and-needs-a-written-budget.md
    # The body the base gate above already read: one read, so the two cannot disagree.
    budget_class=$(echo "$body" | "$agent_python" -m agent_os.lib resolve-budget) \
      || { echo "refusing to dispatch: issue #$issue"; exit 1; }
    echo "budget:    class '$budget_class' (issue #$issue)"

    # The brief is assembled from the tracker, never written by hand: issue body, parent body, and
    # the optional supplement under `## Supplement`.
    brief_args=("$issue" --output "$brieffile")
    [ -n "$extra" ] && brief_args+=(--supplement "$extra")
    "$agent_python" -m agent_os.issues brief "${brief_args[@]}" \
      || { echo "could not assemble the brief for issue #$issue"; exit 1; }
    brief=$brieffile
    echo "$issue" > "$issuefile"
    clear_state_marker
    if "$agent_python" -m agent_os.issues move "$issue" doing; then
      write_state_marker "$issue" "$(project_value labels.doing)"
    else
      echo "WARNING: could not move #$issue to doing; the run starts anyway"
    fi
    after=""
    launch_issue=$issue
  else
    brief=${positional[0]:-$brieffile}
    [ -f "$brief" ] || { echo "no brief to resume from at $brief"; exit 1; }
    # Auto-detect when the caller doesn't say: a run the guard cut resumes as guard_cut, anything
    # else (a manual stop, a crash) resumes as manual.
    if [ "$after" = manual ] && [ -s "$statefile" ] && grep -q '^CUT_BY_GUARD' "$statefile"; then
      after=guard_cut
    fi
    [ -s "$issuefile" ] || echo "WARNING: no recorded issue for this worker (.cache/worker_$backend.issue); resuming without one"

    # THE RELAUNCH CAP, ENFORCED HERE, NOT COUNTED BY THE PLANNER (#362, agent_os/docs/adr/2026-09-14-a-
    # cut-run-is-frozen-in-a-commit-and-only-the-planner-relaunches.md, amended 2026-09-15). A
    # third resume after two guard cuts is not a judgment call for an LLM to get right every time
    # -- it is refused mechanically, before anything is written, so a planner mistake cannot spend
    # a third attempt on a brief that has already failed the same way twice.
    cap_issue=$(cat "$issuefile" 2>/dev/null || true)
    # The same base and fork point every stage decision resolves from; a non-numeric or missing
    # issue returns non-zero and the cap check is skipped, there being nothing to name in a refusal.
    if resolve_stage_context "$cap_issue" && [ -n "$stage_fork_point" ]; then
      # First-parent for the same reason the stage count is: a merged-in base carries other
      # issues' freeze commits, and counting those would refuse a relaunch this issue has earned.
      # What counts as a cut is `subject_is_a_guard_cut`'s call, not this one's: `open-pr`'s
      # pre-merge freeze is a finished run's commit and spends no attempt (#407).
      cap_cut_commits=$(git -C "$worktree" log --first-parent --no-merges --format=%s "$stage_fork_point..HEAD" \
        | count_guard_cut_subjects)
      relaunch_cap=$("$agent_python" -m agent_os.lib planner-value relaunch_cap)
      if [ "$cap_cut_commits" -ge "$relaunch_cap" ]; then
        echo "resume refused: issue #$cap_issue has $cap_cut_commits commits cut by guard" \
          "since forking from $stage_base_ref (>= planner.relaunch_cap=$relaunch_cap) --" \
          "label status:blocked-on-human instead"
        exit 1
      fi
    fi
    launch_issue=$cap_issue
  fi

  launch_stage "$mode" "$launch_issue" "$brief" "$after" "$extra_context"
  ;;

launch-stage)
  # THE CHAIN'S OWN DOOR: the next incomplete stage of the recorded issue, with none of `start`'s
  # gates -- `stage-exit` has just run them all, and this backend's own pidfile still names the
  # process that is calling, so `start`'s "a run is already alive" refusal would be exactly wrong
  # here. Not for a human: `start` and `resume` are the doors.
  launch_stage chain "$(cat "$issuefile" 2>/dev/null || true)" "$brieffile" "" ""
  ;;

stage-exit)
  # A STAGE PROCESS THAT NEVER STARTED IS NOT A STAGE CUT FOR NOT COMMITTING (#381). 127 is what
  # a shell answers when it cannot find the command, in whatever language it says so -- the
  # incident's own log line was `qwen: orden no encontrada` -- and an empty event stream is the
  # other half of "it could not have done any work". First of everything in this case: it needs no
  # `gh` call, no identity and no issue, and it must beat `start`'s two-second settling check to
  # the state file. Deliberately NOT frozen in a `WIP: cut by guard` commit, and that is the
  # point of the distinction: `resume` counts those against `planner.relaunch_cap`, and a
  # dispatch that was never tried may not spend one of the attempts. On #363 this surfaced as
  # `CUT_BY_GUARD reason=no_stage_commit` with `stages: 0/5` -- mechanically true, and the wrong
  # cause.
  backend_status=${WORKER_BACKEND_STATUS:-0}
  if [ "$backend_status" != 0 ] && [ ! -s "$events" ]; then
    # ANY non-zero status, not only 127. The EVENT STREAM is what carries the weight here: a
    # backend that got as far as its first turn writes a line to it, so an empty one proves
    # nothing was tried, whatever the status says. 126 (the path is a directory, or the +x bit is
    # gone), a rejected `--model`, a crash before the first stream-json line -- all of them used
    # to fall through to `CUT_BY_GUARD reason=no_stage_commit`, and on a clean tree that cut
    # freezes no commit, so the relaunch cap never rose and the planner relaunched into the same
    # broken binary for as long as it had wakes.
    case "$backend_status" in
      127) why="the shell could not find it" ;;
      126) why="it is not executable -- a directory, or the +x bit is gone" ;;
      *)   why="it exited $backend_status before writing a single event -- a refused --model, a crash" ;;
    esac
    write_state "FAILED_LAUNCH command=$backend_bin status=$backend_status"
    echo "stage-exit: $backend_bin never started: $why"
    echo "  a failed launch, not a cut: nothing was tried, so it costs no relaunch attempt"
    echo "  fix the binary (project.backends.$backend.command, or project.executables.$backend, in config/agents.yaml) -- not a relaunch"
    exit 1
  fi

  # THE END OF ONE STAGE, decided by the driver and by nothing else (#375). Exits 0 when the next
  # stage is already running in a process group of its own -- the caller must then stop without
  # publishing anything -- and non-zero when this process was the run's end, which is the caller's
  # signal to go on to `open-pr` and the guard's exit hook.
  issue=$(cat "$issuefile" 2>/dev/null || true)
  stage_before=0
  [ -s "$stagefile" ] && stage_before=$(cut -d/ -f1 "$stagefile")

  # A GITHUB APP INSTALLATION TOKEN LASTS AN HOUR. The one this process inherited was minted when
  # its stage began, and a multi-stage run outlives it: on #363 the token minted at 11:21 was
  # already dead by 13:01, so the `gh issue view` below answered 401 and the run was declared
  # finished on a credential that had merely expired. Re-mint before reading anything.
  agent_apply_identity "$(backend_value "$backend" app)" \
    && git -C "$worktree" config credential.helper '!gh auth git-credential'

  # No issue recorded at all is not a failed read -- it is a run that was never staged, and it
  # ends here exactly as it did before. Only a real issue number that could not be READ is a cut.
  case "$issue" in
    ''|*[!0-9]*)
      echo "stage-exit: no issue recorded -- the run ends with this process"
      exit 1
      ;;
  esac

  if ! resolve_stage_context "$issue"; then
    # FAILING TO READ THE ISSUE IS NOT THE ISSUE HAVING NO STAGES. One is a transient fault the
    # planner can relaunch past; the other is a finished run. Conflating them threw away two
    # committed stages on #363. Freeze and cut, so the work stays and the planner decides.
    frozen="tree clean"
    freeze_uncommitted_work issue_unreadable && frozen="uncommitted work frozen in a WIP commit"
    write_state "CUT_BY_GUARD reason=issue_unreadable"
    echo "stage-exit: could not read issue #${issue:-?} -- cut at $stage_before stage(s), not finished"
    post_stage_progress_comment "$issue" "$frozen"
    exit 1
  fi
  if [ "$stages_total" -eq 0 ]; then
    echo "stage-exit: issue #${issue:-?} declares no stages -- the run ends with this process"
    exit 1
  fi

  # Read before the archive below empties the live log: quota is a property of the stream the
  # process that just ended produced.
  quota=$("$agent_python" -m agent_os.lib quota-status "$events" 2>/dev/null || echo allowed)
  archive_stage_events "$issue" "$stages_done"
  printf '%s/%s\n' "$stages_done" "$stages_total" > "$stagefile"

  # WHY A CLEAN EXIT WITHOUT A STAGE COMMIT IS A CUT: the process was given exactly one stage and
  # ended without the commit that closes it, so what it did is unfinished work, not a delivered
  # stage -- and chaining the next stage on top of it would build on a tree nobody has seen.
  # Frozen the same way the guard freezes a run it cuts, so the next process starts from a
  # committed tree (agent_os/docs/adr/2026-09-14-a-cut-run-is-frozen-in-a-commit-and-only-the-planner-
  # relaunches.md).
  if [ "$stages_done" -le "$stage_before" ]; then
    frozen="tree clean"
    freeze_uncommitted_work no_stage_commit && frozen="uncommitted work frozen in a WIP commit"
    write_state "CUT_BY_GUARD reason=no_stage_commit"
    echo "stage-exit: no new stage commit (still $stages_done/$stages_total) -- cut, $frozen"
    post_stage_progress_comment "$issue" "$frozen"
    exit 1
  fi

  if [ "$stages_done" -ge "$stages_total" ]; then
    echo "stage-exit: stage $stages_done/$stages_total was the last one -- the run ends here"
    post_stage_progress_comment "$issue" "$(stage_tree_state)"
    exit 1
  fi

  # THE GATES, IN THIS ORDER: the human's full stop first (it outranks everything and costs one
  # `gh` call), then the backend's own quota (there is no point launching into a wall), then the
  # issue's whole spend against its class ceilings. Both ceilings are the sum of every stage
  # process, not this one's (#375), and EITHER one passing ends the chain: dollars for the roles
  # whose backend reports a cost, tokens for every class, because a Qwen worker's terminal `result`
  # event carries `usage.total_tokens` and no `total_cost_usd` at all -- before #387 the dollar
  # check was the only one here, so on every worker class it measured 0.0000 and never fired.
  reason=""
  budget_note=""
  if agents_paused; then
    reason=paused
  elif [ "$quota" = exhausted ]; then
    reason=quota
  else
    spent=$(issue_cost_usd "$issue")
    cap=$(class_ceiling max_cost_usd) || cap=""
    tokens=$(issue_total_tokens "$issue")
    token_cap=$(class_ceiling max_total_tokens) || token_cap=""
    budget_note="spent \$${spent} of \$${cap:-none} and ${tokens} of ${token_cap:-none} tokens"
    if ceiling_passed "$spent" "$cap" || ceiling_passed "$tokens" "$token_cap"; then
      reason=budget
    fi
  fi
  if [ -n "$reason" ]; then
    # Frozen here too, and for the same reason as any other cut: the next process resumes from a
    # committed tree, and `resume` refuses a dirty worktree outright.
    frozen="tree clean"
    freeze_uncommitted_work "$reason" && frozen="uncommitted work frozen in a WIP commit"
    write_state "CUT_BY_GUARD reason=$reason"
    echo "stage-exit: $stages_done/$stages_total done, not chaining stage $((stages_done + 1)) -- $reason${budget_note:+ ($budget_note)}"
    post_stage_progress_comment "$issue" "$frozen"
    exit 1
  fi

  # A NEW PROCESS GROUP FOR THE NEXT STAGE. `setsid -w` puts the relaunch outside the group this
  # dying process belongs to and waits for it to finish writing the new pid, so that a guard cut
  # landing on the old pgid can neither kill the stage being launched nor leave the pidfile naming
  # a process that no longer exists. The new process writes its own `.pid` and `.state`; this one
  # must touch neither of them after this point.
  echo "stage-exit: $stages_done/$stages_total done -- launching stage $((stages_done + 1)) in a new process group"
  setsid -w "$agent_os_dir/bin/worker_task.sh" "$backend" launch-stage
  ;;

open-pr)
  # THE WORKER'S END. The backend CLI has exited on its own; what it wrote is on a branch in its
  # own worktree and nobody outside can see it yet. Push that branch under the worker's own App
  # identity, open the pull request that says `Closes #N`, and move the issue to `ai-completed`
  # so the planner's next wake has something to hand the validator
  # (agent_os/docs/adr/2026-09-14-a-pr-is-validated-by-a-validator-agent-against-the-issues-acceptance-
  # criteria.md). Called from inside the run's own subshell, BEFORE the guard's exit hook, so the
  # `worker_finished` event never reaches the planner describing a pull request that is not there
  # yet. Every refusal below exits 0: there is nothing to publish, which is not a failure of the
  # run, and the exit hook must still record the run's end.
  state=$(cat "$statefile" 2>/dev/null || true)
  case "$state" in
    CUT_BY_GUARD*|FAILED_LAUNCH*)
      # A cut run's work is already frozen in a `WIP: cut by guard` commit and the planner decides
      # whether it is relaunched; half a task is not a pull request. A run whose backend was never
      # found produced nothing at all (#381), so there is even less to publish -- and the branch
      # it sits on is whatever the dispatch left there.
      echo "open-pr: $backend is $state -- no pull request"; exit 0 ;;
  esac
  [ -e "$worktree/.git" ] || { echo "open-pr: no worktree at $worktree"; exit 0; }
  issue=$(cat "$issuefile" 2>/dev/null || true)
  case "$issue" in ''|*[!0-9]*)
    echo "open-pr: no recorded issue for $backend -- nothing to close, no pull request"; exit 0 ;;
  esac
  branch=$(git -C "$worktree" rev-parse --abbrev-ref HEAD)
  [ "$branch" != HEAD ] || { echo "open-pr: $worktree is on a detached HEAD -- no pull request"; exit 0; }

  # BEFORE THE FIRST `gh` CALL, not after it. Unconditionally, not only when GH_TOKEN is empty: a
  # token inherited from a run that started over an hour ago is present and expired. On #363 the
  # re-mint sat below this read, so the read answered 401, open-pr gave up, and a finished
  # five-stage run never became a pull request.
  if agent_apply_identity "$(backend_value "$backend" app)"; then
    git -C "$worktree" config credential.helper '!gh auth git-credential'
  fi

  issue_body=$(gh issue view "$issue" --json body -q .body) \
    || { echo "open-pr: could not read issue #$issue -- no pull request"; exit 0; }
  # `Base: <branch>` on a line of its own, for a stacked issue whose work does not belong on the
  # trunk; `main` when the issue says nothing, which is the normal case.
  base=$(issue_base_branch "$issue_body")
  [ "$branch" != "$base" ] || { echo "open-pr: $worktree is on $base itself -- no pull request"; exit 0; }

  # What "ahead" is measured against, in order of how well it answers the question: the remote
  # base (what the pull request will actually be diffed against), the local base, and failing
  # both -- a base branch this worktree has never fetched -- the commit this run started from,
  # which at least says whether the worker produced anything at all.
  base_ref=""
  for candidate in "origin/$base" "$base" "$(cat "$startref" 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    git -C "$worktree" rev-parse --verify -q "$candidate^{commit}" >/dev/null 2>&1 || continue
    base_ref=$candidate
    break
  done
  [ -n "$base_ref" ] || { echo "open-pr: neither $base nor a recorded start ref resolves in $worktree -- no pull request"; exit 0; }
  ahead=$(git -C "$worktree" rev-list --count "$base_ref..HEAD" 2>/dev/null || echo 0)
  [ "${ahead:-0}" -gt 0 ] || { echo "open-pr: $branch has no commits ahead of $base_ref -- no pull request"; exit 0; }

  # NO COMMIT MAY CARRY THE DIARY (#407). `scratchpad/progress.log` is the monitor's input, not the
  # task's output, and a branch that adds or modifies it publishes it: it reached `main` that way
  # once, and from then on every worker pull request conflicted with `main` on it -- a conflicting
  # pull request, which is the no-CI case #389 exists to prevent. Refused rather than repaired,
  # because taking a file out of a commit already made is a rewrite and this step rewrites nothing:
  # it names the commits so whoever picks the branch up knows what to take out. Above the freeze
  # and the merge below on purpose -- a refused branch is left exactly as the worker left it, with
  # nothing written to the worktree, to `.state` or to GitHub. Exit 0 with the other refusals:
  # there is nothing to publish, which is not a failure of the run, and the exit hook still records
  # its end. A branch whose diff DELETES the diary passes, which is how a branch forked before
  # `main` stopped tracking it gets clean.
  diary_in_diff=$(git -C "$worktree" diff --name-only --diff-filter=AM "$base_ref" HEAD -- "$DIARY")
  if [ -n "$diary_in_diff" ]; then
    diary_commits=$(git -C "$worktree" log --format='  %h %s' --diff-filter=AM "$base_ref..HEAD" -- "$DIARY")
    echo "open-pr: $branch adds or modifies $DIARY against $base_ref -- no pull request."
    echo "  The diary is the monitor's input and no commit may carry it; these do:"
    if [ -n "$diary_commits" ]; then
      printf '%s\n' "$diary_commits"
    else
      echo "  (no single commit of the branch adds it: it came in through a merge)"
    fi
    echo "  Nothing was written: take $DIARY out of the branch and run open-pr again."
    exit 0
  fi

  # A PULL REQUEST BORN IN CONFLICT GETS NO CI AT ALL (#389). GitHub creates `refs/pull/N/merge`
  # only for a pull request it can merge, and an `on: pull_request` workflow checks out exactly
  # that ref -- so no merge ref means no workflow RUN, not a failing one: the pull request carries
  # ZERO checks and can never meet the merge conditions. Measured on PR #386 (2026-09-16): zero
  # check runs, and `git ls-remote origin 'refs/pull/386/*'` listing only `/head`; merging
  # origin/main into the branch and pushing produced the merge ref and CI started within seconds.
  # Below the `ahead` check on purpose: a branch on the base itself, or with nothing on it, has
  # already exited above, and neither of them has anything to conflict with.
  conflicting_paths=""
  merge_failed=""
  merge_ref=""
  # THE TREE MUST BE COMMITTED BEFORE THE MERGE, or the merge never starts. `git merge` refuses
  # outright when tracked files are modified, and the success arm of `stage-exit` (the last stage
  # landed) is the ONE exit path that does not freeze -- so the tree here is dirty by
  # construction whenever the worker left something behind. A refused merge leaves no unmerged
  # paths, so it used to read as "no conflict" and the branch was pushed UNMERGED: exactly the
  # silent no-CI case this whole step exists to prevent. Frozen with the same `git add -u` every
  # other exit path uses -- never `git add -A`, which has already destroyed a symlink in this
  # repository, so an untracked file that blocks the merge is reported below rather than staged.
  # The one freeze that is NOT a cut: a run only reaches here with every stage committed, so its
  # commit may not spend one of `planner.relaunch_cap`'s attempts, nor be handed to a relaunched
  # stage as unfinished work. Passing the reason as the constant is what keeps the subject this
  # writes and the one `subject_is_a_guard_cut` excludes from drifting apart (#407).
  if [ -n "$(git -C "$worktree" status --porcelain --untracked-files=no)" ]; then
    freeze_uncommitted_work "$PRE_MERGE_FREEZE_REASON" \
      && echo "open-pr: froze uncommitted work in a WIP commit before merging $base"
  fi
  if git -C "$worktree" fetch -q origin "$base" 2>/dev/null; then
    merge_ref=FETCH_HEAD
  elif git -C "$worktree" rev-parse --verify -q "origin/$base^{commit}" >/dev/null 2>&1; then
    merge_ref="origin/$base"
  fi
  if [ -z "$merge_ref" ]; then
    echo "open-pr: could not fetch $base from origin -- pushing $branch without merging it first"
  elif git -C "$worktree" merge-base --is-ancestor "$merge_ref" HEAD; then
    echo "open-pr: $branch already carries every commit of $base -- nothing to merge"
  elif git -C "$worktree" merge --no-edit \
         -m "Merge $base ($(git -C "$worktree" rev-parse --short "$merge_ref")) into $branch before opening the pull request" \
         "$merge_ref" >/dev/null 2>&1; then
    echo "open-pr: merged $base into $branch -- $(git -C "$worktree" log --format='%h %s' -1)"
  else
    # A conflict is what this exists for, but `git merge` can also fail for a reason that is not
    # one (no committer identity, say), and only a conflict leaves unmerged paths behind. Either
    # way the tree goes back exactly as it was -- `--abort` is what makes attempting the merge
    # here safe at all, and resolving a conflict is a human's or a resumed worker's call.
    conflicting_paths=$(git -C "$worktree" diff --name-only --diff-filter=U | sort -u)
    git -C "$worktree" merge --abort 2>/dev/null || true
    if [ -n "$conflicting_paths" ]; then
      echo "open-pr: merging $base into $branch conflicts on:"
      printf '%s\n' "$conflicting_paths" | sed 's/^/  /'
      echo "open-pr: worktree restored -- opening the pull request anyway, because one that exists"
      echo "  and says why it is red beats finished work with no pull request at all"
    else
      # NO UNMERGED PATHS MEANS THE MERGE NEVER STARTED -- an untracked file it would overwrite,
      # a missing committer identity, a broken index. That is NOT "no conflict": pushing now
      # would publish a branch unmerged with its base while reporting nothing wrong, which is the
      # case this step exists to prevent. Stop before the push and say so.
      merge_failed=yes
      write_state "BLOCKED reason=merge_failed base=$base"
      echo "open-pr: merging $base into $branch never started (no unmerged paths, so not a"
      echo "  conflict): an untracked file in the way, or no committer identity. NOT pushing --"
      echo "  a branch pushed unmerged is the silent no-CI case, and this run will not create one."
      git -C "$worktree" status --porcelain | sed 's/^/  /'
      exit 1
    fi
  fi

  # Called standalone (or after a --force start that never minted one), the identity is applied
  # here; called from the run's own subshell it is already in the environment. Missing secrets
  # degrade to the ambient identity with one warning, exactly as `start` does.

  # THE PUSH COMES BEFORE THE LOOKUP, not inside its `else`: the merge above added a commit to
  # this branch, and a pull request that is ALREADY open needs that commit just as much as one
  # about to be created -- it is what produces the `refs/pull/N/merge` ref CI needs (#389). A push
  # of a branch the remote already has is a no-op, so this costs nothing on the common path.
  push_failed=no
  git -C "$worktree" push -u origin "$branch" || push_failed=yes
  if [ "$push_failed" = yes ]; then
    # A REJECTED PUSH MEANS THE REMOTE BRANCH HAS COMMITS THIS WORKTREE DOES NOT -- a human
    # updated it while the run was going. Fast-forward onto them if that is all it is; anything
    # else stops the run, because moving the issue on would declare finished a pull request whose
    # head is not what this run produced, and the two tips would diverge permanently.
    if git -C "$worktree" fetch -q origin "$branch" 2>/dev/null \
       && git -C "$worktree" merge --ff-only FETCH_HEAD >/dev/null 2>&1 \
       && git -C "$worktree" push -u origin "$branch" >/dev/null 2>&1; then
      push_failed=no
      echo "open-pr: the remote $branch was ahead -- fast-forwarded onto it and pushed"
    fi
  fi
  if [ "$push_failed" = yes ]; then
    write_state "BLOCKED reason=push_rejected branch=$branch"
    echo "open-pr: could not push $branch, and fast-forwarding onto the remote was not possible."
    echo "  The issue is left where it is: moving it on would call finished a pull request whose"
    echo "  head is not what this run produced, and the two tips would diverge for good."
    exit 1
  fi

  existing=$(gh pr list --head "$branch" --state open --json number -q '.[0].number' 2>/dev/null || true)
  if [ -n "$existing" ]; then
    echo "open-pr: PR #$existing is already open for $branch"
  else
    title=$(gh issue view "$issue" --json title -q .title) || title="issue #$issue"
    gh pr create --base "$base" --head "$branch" \
      --title "$title (#$issue)" \
      --body "Closes #$issue

Opened by the $backend worker at the end of its run, from $worktree. The acceptance criteria and
the definition of done are in the issue; a validator agent reviews this pull request against them,
and merging stays a human act." \
      || { echo "open-pr: gh pr create failed -- the branch is pushed, the pull request is not"; exit 0; }
  fi

  # GITHUB IS THE AUTHORITY ON `mergeable`, not the merge above: reading it back is how this run
  # says the pull request is in a state CI can act on, instead of assuming the merge made it so.
  # It is computed asynchronously, so `UNKNOWN` is a real answer right after `pr create` and is
  # printed as one rather than read as a conflict (#389).
  # GitHub computes it asynchronously and answers UNKNOWN for the first second or two after
  # `pr create`, so a single read takes the "not conflicting" branch on timing alone. Retried, and
  # a persistent UNKNOWN is reported as NOT VERIFIED rather than quietly as mergeable.
  mergeable=UNKNOWN
  for _attempt in 1 2 3 4 5; do
    mergeable=$(gh pr view "$branch" --json mergeable -q .mergeable 2>/dev/null) || mergeable=UNKNOWN
    [ -n "$mergeable" ] || mergeable=UNKNOWN
    [ "$mergeable" = UNKNOWN ] || break
    sleep 2
  done
  echo "open-pr: mergeable=$mergeable"
  [ "$mergeable" != UNKNOWN ] \
    || echo "open-pr: NOT VERIFIED -- GitHub still answers UNKNOWN, so whether CI will run on this
  pull request has not been confirmed by anything this run did."

  if [ "$mergeable" = CONFLICTING ] || [ -n "$conflicting_paths" ]; then
    # The one ending where the work is done and the pull request still cannot be judged: it is a
    # human's call, so the issue says why in its own comment and carries the label that stops the
    # planner from relaunching anything. TODO(#366): render from project.messages.
    conflict_note="This pull request is in conflict with \`$base\`.

A conflicting pull request gets no \`refs/pull/N/merge\` ref, and an \`on: pull_request\` workflow
checks out exactly that ref: no ref, no workflow run at all. It carries ZERO checks rather than
failing ones, so it cannot meet the merge conditions until the conflict is resolved."
    if [ -n "$conflicting_paths" ]; then
      conflict_note="$conflict_note

Merging \`$base\` into \`$branch\` before pushing conflicted on:
$(printf '%s\n' "$conflicting_paths" | sed 's/^/- `/;s/$/`/')

The merge was aborted and the worktree left exactly as it was."
    else
      conflict_note="$conflict_note

The merge this run made before pushing did not conflict locally, so the base moved between that
merge and GitHub's own answer."
    fi
    "$agent_python" -m agent_os.issues update "$issue" --comment "$conflict_note" \
      || echo "WARNING: could not comment the conflict on #$issue"
    if "$agent_python" -m agent_os.issues move "$issue" blocked-on-human; then
      write_state_marker "$issue" "$(project_value labels.blocked_on_human)"
    else
      echo "WARNING: could not move #$issue to blocked-on-human"
    fi
  elif "$agent_python" -m agent_os.issues move "$issue" ai-completed; then
    write_state_marker "$issue" "$(project_value labels.ai_completed)"
  else
    echo "WARNING: could not move #$issue to ai-completed"
  fi
  ;;

status)
  if alive; then
    pid=$(cat "$pidfile")
    echo "RUNNING pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' ')"
  else
    echo "not running"
  fi
  if [ -s "$startref" ]; then
    base=$(cat "$startref")
    echo "started from $(git -C "$worktree" rev-parse --short "$base") on $(git -C "$worktree" rev-parse --abbrev-ref HEAD), $(git -C "$worktree" rev-list --count "$base"..HEAD) commit(s) since"
  fi
  [ -s "$statefile" ] && echo "state:     $(cat "$statefile")"
  # The one state whose remedy is not "relaunch it": nothing ran, so there is no run to continue,
  # and the `command=` half names exactly what could not be found (#381).
  case "$(head -n1 "$statefile" 2>/dev/null || true)" in
    FAILED_LAUNCH*)
      echo "           that command was never found, so this run never started: it cost no"
      echo "           relaunch attempt, and the fix is project.backends.$backend.command (or its"
      echo "           fallback, project.executables.$backend) in config/agents.yaml"
      ;;
  esac
  [ -s "$issuefile" ] && echo "issue:     #$(cat "$issuefile")"
  # Stages done / total, derived from the branch's own commits when the current process was
  # launched (#375). A live process is working on the one after those, which is what makes the
  # count and the process together the whole answer to "how far has this issue got".
  if [ -s "$stagefile" ]; then
    if alive; then
      echo "stages:    $(cat "$stagefile") (this process: stage $(($(cut -d/ -f1 "$stagefile") + 1)))"
    else
      echo "stages:    $(cat "$stagefile")"
    fi
  fi
  echo "--- tokens ---"; usage_report
  # Keep the session id fresh so `resume` works even after a crash.
  sid=$(grep -o '"session_id":"[^"]*"' "$events" 2>/dev/null | tail -1 | cut -d'"' -f4)
  [ -n "$sid" ] && echo "$sid" > "$sidfile"
  echo "--- last assistant text ---"
  "$agent_python" - "$events" <<'PY' 2>/dev/null || true
import json, sys, pathlib
texts = []
for line in pathlib.Path(sys.argv[1]).read_text(errors="replace").splitlines():
    try:
        event = json.loads(line)
    except ValueError:
        continue
    for block in ((event.get("message") or {}).get("content") or []):
        if block.get("type") == "text" and block.get("text", "").strip():
            texts.append(block["text"].strip())
print("\n".join(texts[-2:])[-1200:] if texts else "  (nothing yet)")
PY
  ;;

watch)  tail -f "$events" ;;

collect)
  base=$(cat "$startref" 2>/dev/null) || { echo "no recorded start ref"; exit 1; }
  alive && echo "NOTE: the run is still alive (pid $(cat "$pidfile")); this is a snapshot."
  echo "=== commits ==="
  # `--first-parent --no-merges` here and for the file list below: since #389 this branch may
  # carry a merge of its own base, and a plain `diff <start>..HEAD` then reports the worker for
  # every file the base moved -- `docs/adr/*` and the rest of `project.forbidden_paths` included,
  # so a clean run failed its own ownership audit. What the audit judges is what THIS branch's own
  # commits wrote (#389 review).
  git -C "$worktree" log --first-parent --no-merges --oneline "$base"..HEAD
  changed=$(git -C "$worktree" log --first-parent --no-merges --name-only --format= "$base"..HEAD \
    | sort -u | grep . || true)
  echo "=== changed files ==="; echo "$changed" | sed 's/^/  /'
  echo "=== ownership audit ==="
  # Two lists and one rule each (agent_os/docs/adr/2026-09-16-the-mechanisms-own-files-are-not-the-host-
  # projects-protected-paths.md): the host project's paths hold whatever the brief says, and the
  # mechanism's own files yield to a body that names them. Only the second half reads that body, so
  # the lookup stays on the rare path -- a run touching no mechanism file never opens it.
  host_violations=""
  if [ -z "$FORBIDDEN" ]; then
    # `project.forbidden_paths` is empty: no pattern to audit with, and an empty one would flag
    # every path this run touched.
    echo "  nothing forbidden: project.forbidden_paths is empty"
  else
    host_violations=$(echo "$changed" | grep -E "$FORBIDDEN" || true)
    if [ -n "$host_violations" ]; then
      echo "  VIOLATION -- these paths were off limits:"; echo "$host_violations" | sed 's/^/    /'
    else
      echo "  clean: nothing outside the agent's own paths"
    fi
  fi
  if [ -z "$MECHANISM" ]; then
    echo "  nothing of the mechanism's own is protected: mechanism.own_paths is empty"
  else
    mechanism_touched=$(echo "$changed" | grep -E "$MECHANISM" || true)
    # A path on BOTH lists stays refused by the host half whatever the body says: dropping it here
    # keeps the second half from printing an "allowed" line for a path the first one just refused.
    if [ -n "$mechanism_touched" ] && [ -n "$host_violations" ]; then
      mechanism_touched=$(printf '%s\n' "$mechanism_touched" | grep -vxF "$host_violations" || true)
    fi
    if [ -z "$mechanism_touched" ]; then
      echo "  clean: no file of the mechanism's own touched"
    else
      named="" unnamed=""
      while IFS= read -r touched_path; do
        [ -n "$touched_path" ] || continue
        if issue_body_names_path "$touched_path"; then
          named="$named$touched_path"$'\n'
        else
          unnamed="$unnamed$touched_path"$'\n'
        fi
      done <<< "$mechanism_touched"
      if [ -n "$named" ]; then
        echo "  allowed -- the issue body names these files of the mechanism's own:"
        printf '%s' "$named" | sed 's/^/    /'
      fi
      if [ -n "$unnamed" ]; then
        # No body on disk means nothing can have been authorized, and the audit says which of the
        # two it is rather than let a missing file read as an unnamed one.
        if [ -s "$bodyfile" ]; then
          echo "  VIOLATION -- files of the mechanism's own the issue body does not name:"
        else
          echo "  VIOLATION -- files of the mechanism's own, and no issue body at $bodyfile to authorize one:"
        fi
        printf '%s' "$unnamed" | sed 's/^/    /'
      fi
    fi
  fi
  echo "=== uncommitted work left behind ==="
  git -C "$worktree" status --porcelain | sed 's/^/  /'
  echo "=== main checkout untouched? ==="
  stray=$(git -C "$main" status --porcelain)
  if [ -n "$stray" ]; then echo "  CHECK -- the main tree has changes (yours or the worker's?):"; echo "$stray" | sed 's/^/    /'; else echo "  clean"; fi
  echo "=== deliverables ==="
  echo "$changed" | grep '^scratchpad/' | sed 's/^/  /' || echo "  none"
  # An empty commit list and an empty deliverable list read as "the agent found nothing" and as
  # "the agent never ran" in exactly the same way. Say which one it is.
  [ -z "$(git -C "$worktree" log --first-parent --no-merges --oneline "$base"..HEAD)" ] && echo "  NOTE: no commits since the start ref -- read the RESULT line below before calling this a clean report."
  echo "=== cost ==="; usage_report
  ;;

stop)
  alive || { echo "not running"; exit 0; }
  pid=$(cat "$pidfile"); pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
  echo "stopping pid $pid (group $pgid)"
  kill -TERM -"$pgid"
  for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || { echo stopped; exit 0; }; sleep 1; done
  echo "still alive after 20s, sending KILL"; kill -KILL -"$pgid"
  ;;

freeze)
  # The guard's door into `freeze_uncommitted_work` (#482). `agent_guard.cut_run` calls this right
  # after `stop`, the way it already calls `stop` for the kill, instead of keeping its own
  # `git add -u`: that second freeze never received #417's untracked half, so a run a tick cut left
  # the files it had written behind its own `WIP: cut by guard` commit -- the one state `resume`
  # refuses. No `alive` check on purpose. The run this freezes is the one `stop` was just asked to
  # kill, and refusing the tree because its process is still dying would drop the freeze in exactly
  # the case it exists for.
  reason=${2:-}
  [ -n "$reason" ] || { echo "usage: $0 $backend freeze <reason>"; exit 2; }
  [ -e "$worktree/.git" ] || { echo "no worktree at $worktree"; exit 1; }
  if freeze_uncommitted_work "$reason"; then
    git -C "$worktree" log -1 --format='frozen %h %s'
  else
    echo "nothing to freeze at $worktree"
  fi
  ;;

*) sed -n '2,35p;37,44p' "$0"; exit 2 ;;
esac
