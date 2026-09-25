#!/usr/bin/env bash
# workaround titanarq/agent-os#18: `worker_task.sh start` reads a finished run's leftover
# scratchpad/ (its progress.log diary and any scratch files) as uncommitted work and refuses the
# next dispatch on that backend.
# Usage: scripts/clean_stale_worker.sh [--dry-run] <backend>   (e.g. qwen)
# Refuses unless the worktree's only dirt is scratchpad/ (and the driver's .env link) and HEAD is
# proven merged into origin/main; then archives scratchpad/ to .cache/stale_diaries/, detaches HEAD
# at origin/main and deletes that issue's own branches proven merged. --dry-run runs every check
# and says what it would do, touching nothing but the fetch. Remove once agent-os#18 is fixed.
#
# "Merged" is squash-aware (#187, titanarq/agent-os#71): squash merges are the policy, so a
# worker's commits are never ancestors of origin/main. HEAD counts as merged when, first match:
#   1. ancestry: HEAD is an ancestor of origin/main (a merge commit or fast-forward);
#   2. pr: the branch has a MERGED pull request whose head contains HEAD -- ONE REST call,
#      `gh api repos/<repo>/pulls?head=<owner>:<branch>&state=closed` (merged_at not null). A
#      commit made after the merge is not contained, so it falls through to 3;
#   3. content: `git diff --quiet origin/main HEAD -- <paths the branch touched>` -- every file the
#      branch changed reads on origin/main exactly as on HEAD.
# Otherwise it is real unmerged work and the script refuses, exactly as before.
#
# Overrides (tests only): SA_STALE_MAIN (main checkout), SA_STALE_GH (gh executable),
# SA_STALE_REPO (owner/name; default parsed from the main checkout's origin URL).
set -euo pipefail
dry_run=no
[ "${1:-}" = "--dry-run" ] && { dry_run=yes; shift; }
backend=${1:?usage: $0 [--dry-run] <backend>}
main=${SA_STALE_MAIN:-$(cd "$(dirname "$0")/.." && pwd)}
gh_bin=${SA_STALE_GH:-gh}
wt="$main/../studentassistant-$backend"
[ -d "$wt" ] || { echo "no worktree $wt"; exit 1; }
g() { git -C "$wt" "$@"; }

repo=${SA_STALE_REPO:-}
if [ -z "$repo" ]; then
  repo=$(git -C "$main" remote get-url origin | sed -E 's#^(git@[^:]+:|https?://[^/]+/)##; s#\.git$##')
fi

g fetch -q --prune origin
# Every untracked file listed on its own (-uall): a collapsed `?? scratchpad/` must not hide what
# is inside it, and anything outside scratchpad/ is work the worker left.
dirt=$(g status --porcelain --untracked-files=all | grep -v -E -e '^\?\? scratchpad/' -e '^\?\? \.env$' || true)
[ -z "$dirt" ] || { echo "real uncommitted work, refusing:"; echo "$dirt"; exit 1; }

branch=$(g symbolic-ref -q --short HEAD || true)
merged_by=""
if g merge-base --is-ancestor HEAD origin/main; then
  merged_by="ancestry"
fi
if [ -z "$merged_by" ] && [ -n "$branch" ]; then
  if prs=$("$gh_bin" api "repos/$repo/pulls?head=${repo%%/*}:$branch&state=closed&per_page=100" \
             --jq '.[] | select(.merged_at != null) | "\(.number) \(.head.sha)"' 2>/dev/null); then
    while read -r num sha; do
      [ -n "$num" ] || continue
      # The PR's head may be gone from origin (branch deleted on merge): GitHub keeps it as
      # refs/pull/<n>/head.
      g cat-file -e "$sha^{commit}" 2>/dev/null || g fetch -q origin "refs/pull/$num/head" 2>/dev/null || true
      if g merge-base --is-ancestor HEAD "$sha" 2>/dev/null; then
        merged_by="pr #$num (merged)"
        break
      fi
    done <<< "$prs"
  else
    echo "note: could not look up pull requests for $branch; falling back to a content check"
  fi
fi
if [ -z "$merged_by" ]; then
  fork=$(g merge-base origin/main HEAD)
  mapfile -t paths < <(g diff --name-only "$fork" HEAD)
  if [ "${#paths[@]}" -eq 0 ] || g diff --quiet origin/main HEAD -- "${paths[@]}"; then
    merged_by="content (no difference from origin/main in the ${#paths[@]} path(s) the branch touched)"
  fi
fi
if [ -z "$merged_by" ]; then
  echo "HEAD has commits not on origin/main (no merged PR contains them, content differs), refusing:"
  g log --oneline origin/main..HEAD
  exit 1
fi
echo "$backend: ${branch:-detached HEAD} is merged into origin/main by $merged_by"

issue=$(cat "$main/.cache/worker_$backend.issue" 2>/dev/null || echo unknown)
# Only the finished issue's own branches (branches are shared by every worktree of the repo): the
# ones origin/main reaches by ancestry, plus the branch HEAD was on, proven merged above.
doomed=$(g branch --format='%(refname:short)' --merged origin/main | grep -E "^[^/]+/$issue-" || true)
if [ -n "$branch" ] && [[ "$branch" =~ ^[^/]+/$issue- ]] && ! grep -qxF "$branch" <<< "$doomed"; then
  doomed=$(printf '%s\n%s' "$doomed" "$branch" | sed '/^$/d')
fi

if [ "$dry_run" = yes ]; then
  [ -d "$wt/scratchpad" ] && echo "would archive $wt/scratchpad to $main/.cache/stale_diaries/"
  echo "would detach $wt at origin/main ($(g rev-parse --short origin/main))"
  for b in $doomed; do echo "would delete branch $b"; done
  exit 0
fi

if [ -d "$wt/scratchpad" ]; then
  mkdir -p "$main/.cache/stale_diaries"
  mv "$wt/scratchpad" "$main/.cache/stale_diaries/$backend-issue$issue-$(date +%Y%m%dT%H%M%S).scratchpad"
fi
g switch -q --detach origin/main
for b in $doomed; do
  g branch -q -D "$b" && echo "deleted merged branch $b"
done
g status --short --branch
