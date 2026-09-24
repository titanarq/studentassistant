#!/usr/bin/env bash
# workaround titanarq/agent-os#18: `worker_task.sh start` reads a finished run's leftover
# scratchpad/progress.log as uncommitted work and refuses the next dispatch on that backend.
# Usage: scripts/clean_stale_worker.sh <backend>   (e.g. qwen)
# Refuses unless the worktree's only dirt is the diary and HEAD has nothing unmerged into
# origin/main; then archives the diary to .cache/stale_diaries/, detaches HEAD at origin/main
# and deletes that issue's own branches already merged into it. Remove once agent-os#18 is fixed.
set -euo pipefail
backend=${1:?usage: $0 <backend>}
main=$(cd "$(dirname "$0")/.." && pwd)
wt="$main/../studentassistant-$backend"
[ -d "$wt" ] || { echo "no worktree $wt"; exit 1; }
git -C "$wt" fetch -q --prune origin
dirt=$(git -C "$wt" status --porcelain | grep -v -x -e '?? scratchpad/' -e '?? scratchpad/progress.log' -e '?? .env' || true)
[ -z "$dirt" ] || { echo "real uncommitted work, refusing:"; echo "$dirt"; exit 1; }
ahead=$(git -C "$wt" log --oneline origin/main..HEAD)
[ -z "$ahead" ] || { echo "HEAD has commits not on origin/main, refusing:"; echo "$ahead"; exit 1; }
issue=$(cat "$main/.cache/worker_$backend.issue" 2>/dev/null || echo unknown)
if [ -f "$wt/scratchpad/progress.log" ]; then
  mkdir -p "$main/.cache/stale_diaries"
  mv "$wt/scratchpad/progress.log" "$main/.cache/stale_diaries/$backend-issue$issue-$(date +%Y%m%dT%H%M%S).progress.log"
  rmdir "$wt/scratchpad" 2>/dev/null || true
fi
git -C "$wt" switch -q --detach origin/main
# Only the finished issue's own branches (branches are shared by every worktree of the repo).
for b in $(git -C "$wt" branch --format='%(refname:short)' --merged origin/main | grep -E "^[^/]+/$issue-" || true); do
  git -C "$wt" branch -q -d "$b" && echo "deleted merged branch $b"
done
git -C "$wt" status --short --branch
