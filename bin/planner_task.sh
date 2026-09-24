#!/usr/bin/env bash
# Drives the PLANNER: a one-shot headless run, from the main checkout only (never a worktree --
# it never edits code), that reads what the monitor tick just reported plus the tracker's own
# state and decides what happens next: relaunch a cut/blocked worker, dispatch a queued issue,
# freeze one at its relaunch cap, or page a human. Unlike a worker it is never resumed -- one
# invocation is one decision, per agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-
# never-as-a-standing-process.md ("the planner itself runs to a decision and exits too").
#
# The backend it runs on is the `planner` class's own, unless the launch gate substitutes the
# fallback that class declares because the guard's persisted quota verdict reads `exhausted` (#425)
# -- which is what keeps a shut Claude window from stopping the one role that redispatches
# everything else. The identity lines and the log header below say which of the two ran.
#
#   agent_os/bin/planner_task.sh run ["<context>"]
#   agent_os/bin/planner_task.sh rules            # the resolved RULES block, and nothing else
#
# Never call this directly from a trigger: `agent_os.guard wake` is the one door, and it
# is what holds `.cache/planner.lock` so two planners cannot act on the tracker at once.
#
# <context> is the list of events that woke the planner, read off `.cache/planner_events/` by
# `wake` -- folded into the planner's first prompt so it has somewhere to start without
# re-deriving it. Omit it for a manual invocation.
#
# **Writes**: one log per run, `.cache/planner/<utc-timestamp>.log` (never truncated, never
# reused), and one appended line per run in `.cache/planner/runs.tsv` (ts, context, model, turns,
# cost). `PLANNER_CACHE_DIR` moves both, for a dry run against a stub backend.
set -uo pipefail

# The identity block, the role's model and the runs.tsv row are the same three steps for every
# role, so they live once, in the one-shot driver, and are sourced rather than copied here
# (sourcing it defines the `agent_*` helpers, the resolved interpreter and the HOST project's root,
# and returns before its own dispatch).
# shellcheck source=agent_os/bin/agent_task.sh
source "$(dirname "${BASH_SOURCE[0]}")/agent_task.sh"
main=$agent_main

python=$agent_python

run_stamp=$(date -u +%Y%m%dT%H%M%SZ)
planner_dir=${PLANNER_CACHE_DIR:-$(agent_run_dir planner)}
logfile=$planner_dir/$run_stamp.log
runs_tsv=$planner_dir/runs.tsv
mkdir -p "$planner_dir"

# Worktrees, identities and every other project-specific value come from config/agents.yaml's
# `project:` section -- agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-
# configured-not-coded.md.
# One `--add-dir` per configured backend worktree, so the planner can read every worker's tree --
# the backends' own list, never a hardcoded pair of names (#514). `WORKER_WORKTREE_<BACKEND>`
# overrides one of them, as `WORKER_WORKTREE_QWEN` / `WORKER_WORKTREE_CLAUDE` always did.
planner_worktree_dirs=()
while IFS= read -r worktree_backend; do
  [ -n "$worktree_backend" ] || continue
  worktree_override=WORKER_WORKTREE_$(printf '%s' "$worktree_backend" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')
  worktree_dir=${!worktree_override:-$(cd "$main" && "$python" -m agent_os.lib worktree-path "$worktree_backend")}
  planner_worktree_dirs+=(--add-dir "$worktree_dir")
done < <(cd "$main" && "$python" -m agent_os.lib worktree-backends)

# The backend CLI, from `project.executables` when the project configures one and from PATH
# otherwise (#380) -- wrapped so a stub `claude` first on PATH (echoing its argv and emitting one
# fake `{"type":"result",...}` line) exercises this script end to end without spending a turn.
#
# WHICH backend that is comes from the launch gate, not from the class's own `backend:` field read
# a second time (#425): `agent_read_launch_gate` sets `launch_backend`, `model`,
# `launch_substituted` and `launch_reason` off the guard's persisted verdict on Claude's quota, so
# a planner woken while that window is shut runs on the class's declared fallback instead of being
# rejected before its first turn -- which is what happened at 08:48:46Z on 2026-09-18, to the one
# role whose job was redispatching everything else. One environment override per backend, so a test
# stubs the fallback without stubbing Claude's and can still tell which of the two ran.
agent_read_launch_gate planner || exit 1
agent_set_backend_flags "$launch_backend" || {
  echo "the launch gate named a backend this driver cannot run: '$launch_backend'"
  exit 1
}
planner_bin_override=$(agent_backend_bin_variable PLANNER "$launch_backend")
planner_backend_bin=$(agent_executable "$launch_backend" "${!planner_bin_override:-}") || exit 1
planner_cli() { "$planner_backend_bin" "$@"; }

# Injected into every run. The planner is a different kind of actor from a worker: it decides what
# happens next, it never does the work itself. The text is `agent_os/prompts/planner.md` plus
# whatever `project.prompt_extras.planner` appends at its extension point, with every placeholder
# the project's own config answers filled in by the renderer -- the planner has none that only the
# run knows (#509). A prompt that will not render is a loud stop here rather than a backend
# launched on a half-written one.
RULES=$("$python" -m agent_os.lib render-prompt planner) || exit 1

case "${1:-}" in
rules)
  # The resolved block, for reading and for a test -- no identity minted, no backend called.
  echo "$RULES"
  ;;
run)
  context=${2:-"(no context given -- manual invocation)"}

  # GitHub identity: its own App, distinct from a worker's backend identity, so a planning
  # decision reads as one at a glance -- agent_os/docs/adr/2026-09-14-the-planner-has-its-own-github-
  # identity-separate-from-a-workers-backend-identity.md. Both the slug and where its secrets live
  # come from config/agents.yaml's `project:` section. Missing secrets degrade to one warning and
  # the ambient gh/git identity, the same style worker_task.sh uses -- never a hard failure.
  agent_apply_identity "$("$python" -m agent_os.lib role-app planner)"

  # The planner's own budget class (cheap and short -- it only reads evidence, labels, freezes or
  # relaunches, never writes code) names the model, and the launch gate above has already resolved
  # it together with the backend that runs it: `model` here is the gate's answer, which is the
  # class's own model on an unsubstituted run and the fallback's on a substituted one (#425).
  # Nothing is hardcoded here a second time, and the class's own backend is read only to say what
  # a substitution was a substitution FOR.
  class_backend=$(agent_role_field planner backend) \
    || { echo "could not resolve the planner's own class from config/agents.yaml"; exit 1; }
  agent_backend_identity_line "$class_backend"
  echo "model:     $model"
  echo "launch:    $launch_reason"
  echo "context:   $context"

  # One fresh log per run, never truncated and never reused: the previous behaviour (`: > .cache/
  # planner.log`) meant the only evidence of what a planner did was gone the moment the next one
  # started -- which is exactly how 51 runs in six hours went unnoticed (#345).
  {
    echo "ts:        $run_stamp"
    agent_backend_identity_line "$class_backend"
    echo "model:     $model"
    echo "launch:    $launch_reason"
    echo "context:   $context"
  } >>"$logfile"
  # `--add-dir`, `--model` and `--append-system-prompt` are the same words on both backends; the
  # flags that are not come from the one array `agent_set_backend_flags` fills (#425).
  planner_cli "${agent_backend_flags[@]}" --model "$model" \
    "${planner_worktree_dirs[@]}" \
    --append-system-prompt "$RULES" \
    "You have been woken by agent_os.guard. The events since the last planner run: $context. Act on them and exit." \
    >>"$logfile" 2>&1
  agent_mark_backend_exited "$logfile" >>"$logfile" 2>&1

  # One line per run in runs.tsv -- what woke it, what it cost. Parsed from the log's last
  # `result` event by agent_lib, so there is one implementation of "what did this run cost". The
  # model column carries the model that RAN, which is what keeps a substituted run's tokens out of
  # the Claude total whoever reads this file next reports (`agent_os/docs/AGENT_OS.md` §3).
  agent_append_run_row "$logfile" "$runs_tsv" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$context" "$model"
  echo "log $logfile"
  ;;
*) sed -n '2,15p' "$0"; exit 2 ;;
esac
