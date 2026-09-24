#!/usr/bin/env bash
# workaround titanarq/agent-os#18 (copied from teachermovies, where decision A on its #1 chose it, 2026-09-24): the *dispatch* side of
# scripts/clean_stale_worker.sh. That script clears a finished run's leftover
# scratchpad/progress.log so the next `worker_task.sh <backend> start` stops refusing on it, but
# something still has to decide WHEN it is safe to run it -- by hand, someone forgets, and #18
# recurs on the very next dispatch after every merge. This is the second `ExecStart=` of
# scripts/systemd/studentassistant-board-sync.service, so it rides the same 5-minute timer as the
# board sync with no separate unit to maintain (host-owned, outside `agent_os/`).
#
# For every backend `worktree-backends` lists (today: qwen, claude), runs
# `scripts/clean_stale_worker.sh <backend>` IF AND ONLY IF:
#   - `.cache/worker_<backend>.issue` names an issue, and
#   - that issue is CLOSED on GitHub (an open issue is a run still in flight: never touch it).
# `clean_stale_worker.sh` itself is the guard against the other two dangers named in issue #18's
# workaround (operations.md): it separately refuses when the worktree carries any uncommitted
# file besides the diary, or any commit on HEAD not yet on `origin/main` -- both checked again
# here first so a GitHub hiccup or an ineligible worktree never even shells out to `gh issue view`
# or touches the worktree. It only ever detaches HEAD and deletes branches already MERGED into
# origin/main (`--merged`), so an unmerged branch is never at risk from either script.
#
# Idempotent and quiet: nothing eligible (no issue file, issue still open, worktree already idle,
# or already cleaned) prints nothing and exits 0. Only an actual cleanup, or a real refusal
# (uncommitted work / unpushed commits, which `clean_stale_worker.sh` reports on its own),
# writes anything. Safe to run every 5 minutes forever; running it twice in a row after a
# cleanup is a no-op the second time (the worktree is already detached at origin/main with no
# diary, so the "nothing eligible" check above skips it).
#
# Remove alongside scripts/clean_stale_worker.sh once agent-os#18 is fixed upstream and the
# subtree is pulled (see docs/runbooks/operations.md).
set -uo pipefail

main=$(cd "$(dirname "$0")/.." && pwd)
cd "$main" || exit 1

# shellcheck source=agent_os/bin/agent_task.sh
source "$main/agent_os/bin/agent_task.sh" >/dev/null 2>&1

gh_bin=$(agent_executable gh) || { echo "clean_stale_workers_if_closed: could not resolve gh" >&2; exit 1; }
repo=$(agent_project_value repo) || { echo "clean_stale_workers_if_closed: could not resolve project.repo" >&2; exit 1; }

backends=$("$agent_python" -m agent_os.lib worktree-backends) || {
  echo "clean_stale_workers_if_closed: could not list project.backends" >&2
  exit 1
}

status=0
for backend in $backends; do
  issuefile="$main/.cache/worker_$backend.issue"
  [ -s "$issuefile" ] || continue
  issue=$(tr -d '[:space:]' < "$issuefile")
  [[ "$issue" =~ ^[0-9]+$ ]] || continue

  wt="$main/../studentassistant-$backend"
  [ -d "$wt" ] || continue

  # Nothing to clean if the worktree is already idle (detached, no diary) -- the common case on
  # every tick between cleanups. Skip quietly rather than re-running the heavier checks below.
  branch=$(git -C "$wt" symbolic-ref -q --short HEAD || true)
  [ -n "$branch" ] || [ -f "$wt/scratchpad/progress.log" ] || continue

  state=$("$gh_bin" issue view "$issue" --repo "$repo" --json state -q .state 2>/dev/null) || {
    echo "clean_stale_workers_if_closed: could not read state of #$issue ($backend), skipping" >&2
    status=1
    continue
  }
  [ "$state" = "CLOSED" ] || continue

  "$main/scripts/clean_stale_worker.sh" "$backend" || status=1
done

exit $status
