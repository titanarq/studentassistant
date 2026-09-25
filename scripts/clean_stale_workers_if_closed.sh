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
# workaround (operations.md): it refuses when the worktree carries any uncommitted file outside
# scratchpad/, or when HEAD is not proven merged into origin/main. "Merged" is squash-aware
# (#187): ancestry, else a merged pull request of the branch containing HEAD (one REST call),
# else no content difference from origin/main in the paths the branch touched -- so a
# squash-merged issue is cleaned and real unmerged work is still refused.
#
# GitHub is read over REST only (`gh api`), never GraphQL: one call for the issue state here and
# at most one pull-request lookup in `clean_stale_worker.sh`, and only for a worktree that is not
# already idle.
#
# Usage: scripts/clean_stale_workers_if_closed.sh [--dry-run]   (--dry-run is passed through:
# every check runs, nothing in a worktree is touched).
#
# Idempotent and quiet: nothing eligible (no issue file, issue still open, worktree already idle,
# or already cleaned) prints nothing and exits 0. Only an actual cleanup, or a real refusal
# (uncommitted work / unmerged commits, which `clean_stale_worker.sh` reports on its own),
# writes anything. Safe to run every 5 minutes forever; running it twice in a row after a
# cleanup is a no-op the second time (the worktree is already detached at origin/main with no
# diary, so the "nothing eligible" check above skips it).
#
# Remove alongside scripts/clean_stale_worker.sh once agent-os#18 is fixed upstream and the
# subtree is pulled (see docs/runbooks/operations.md).
set -uo pipefail

dry_run=()
[ "${1:-}" = "--dry-run" ] && dry_run=(--dry-run)

here=$(cd "$(dirname "$0")" && pwd)
# SA_STALE_MAIN: the main checkout whose .cache/ and sibling worktrees are read (default: this
# script's own checkout) -- lets a lane worktree dry-run its copy against the live workers.
main=${SA_STALE_MAIN:-$(cd "$here/.." && pwd)}
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
  [ -n "$branch" ] || [ -d "$wt/scratchpad" ] || continue

  state=$("$gh_bin" api "repos/$repo/issues/$issue" --jq .state 2>/dev/null) || {
    echo "clean_stale_workers_if_closed: could not read state of #$issue ($backend), skipping" >&2
    status=1
    continue
  }
  [ "$state" = "closed" ] || continue

  SA_STALE_MAIN="$main" SA_STALE_GH="$gh_bin" SA_STALE_REPO="$repo" "$here/clean_stale_worker.sh" "${dry_run[@]}" "$backend" \
    || status=1
done

exit $status
