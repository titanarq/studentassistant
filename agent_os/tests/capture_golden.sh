#!/usr/bin/env bash
# Renders every role's prompt exactly as its own driver hands it to a backend, and writes each one
# to `agent_os/tests/golden/<role>.md` (or to the directory given as the first argument).
#
#   bash agent_os/tests/capture_golden.sh [output-dir]
#
# It exists so a refactor of where the prompt text LIVES can be proved to have changed nothing an
# agent reads: capture before, capture after, diff. The files it writes are the only place under
# `agent_os/` allowed to carry a host project's literals, because they are a recording of one
# host's rendered prompt and not text the mechanism ships (#509, #510).
#
# Nothing here launches a backend or mints an identity: each driver already has a mode that
# resolves its prompt and prints it -- `worker_task.sh <backend> rules`, `planner_task.sh rules`,
# and `agent_task.sh <role> <subject> --dry-run`, whose output carries the block after its own
# `--- rules ---` marker.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
bin=$here/../bin
# The git checkout this script itself lives in -- NOT `$AGENT_OS_HOST_ROOT` (a caller, e.g. this
# suite's `captured` fixture, may point that at a throwaway host with no `.git` of its own, built
# only to carry `config.example.yaml`): `git rev-parse --show-toplevel` from here, falling back to
# the same two-levels-up guess `host_root()` uses when there is no git repository at all (#512 --
# out-of-tree, `agent_os/` IS the checkout, not nested one level inside a bigger one).
root=$(cd "$here" && git rev-parse --show-toplevel 2>/dev/null) || root=$(cd "$here/../.." && pwd)
out=${1:-$here/golden}
mkdir -p "$out"

# Two things would otherwise make a captured prompt true only on the machine that captured it.
# The main checkout's absolute path is substituted into the worker's rules, so it is replaced by a
# sentinel no placeholder syntax can be confused with; and a live quota verdict in the host's real
# `.cache/` decides which backend a one-shot role resolves to, so the drivers are pointed at an
# empty cache directory that holds none.
#
# The sentinel has to be computed from wherever the drivers below will ACTUALLY land, not always
# `$root`: `worker_task.sh`'s own `main_checkout()` derives the checkout from `$PWD` after
# `agent_task.sh`'s `cd "$agent_main"`, and `agent_main` is `$AGENT_OS_HOST_ROOT` when a caller set
# one -- the `captured` fixture's throwaway host, #512 -- not `$root`. Computing it from `$root`
# regardless meant the substitution below matched nothing there, and a bare "." (the same defect
# `main_checkout()` was fixed against, #512 follow-up) leaked straight into the golden file. Fails
# loudly rather than the historic `dirname "$(failing-command)"` pattern, which still exits 0 on
# an empty argument and would silently normalize nothing.
checkout_root=${AGENT_OS_HOST_ROOT:-$root}
common_dir=$(cd "$checkout_root" && git rev-parse --path-format=absolute --git-common-dir) || {
  echo "capture_golden: $checkout_root is not a git repository" >&2
  exit 1
}
main_checkout=$(dirname "$common_dir")
cache=$(mktemp -d)
trap 'rm -rf "$cache"' EXIT
export WORKER_CACHE_DIR=$cache

normalized() { sed "s|$main_checkout|<HOST-MAIN-CHECKOUT>|g"; }

cd "$root"
bash "$bin/worker_task.sh" claude rules | normalized >"$out/worker.md"
bash "$bin/planner_task.sh" rules | normalized >"$out/planner.md"
for role in validator refiner; do
  bash "$bin/agent_task.sh" "$role" 1 --dry-run |
    sed -n '/^--- rules ---$/,$p' | tail -n +2 | normalized >"$out/$role.md"
done

for role in worker validator refiner planner; do
  [ -s "$out/$role.md" ] || { echo "capture_golden: $out/$role.md came out empty"; exit 1; }
  printf '%s\t%s lines\n' "$out/$role.md" "$(wc -l <"$out/$role.md")"
done
