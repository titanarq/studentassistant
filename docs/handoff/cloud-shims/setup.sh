#!/usr/bin/env bash
# Recreates, in a fresh Claude Code cloud container, the workarounds the 2026-09-28 cloud session
# used to run agent-os workers there. Nothing here is meant for the local PC or for `main`.
# Run from the repository root of the cloud checkout: bash <path>/setup.sh
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(git rev-parse --show-toplevel)

# 1. The real gh CLI (REST only works: GraphQL is blocked in cloud sessions), moved aside, and the
#    shim that translates the `gh issue|pr ...` calls agent-os makes into REST in its place.
tmp=$(mktemp -d)
curl -sSL -o "$tmp/gh.tgz" https://github.com/cli/cli/releases/download/v2.63.2/gh_2.63.2_linux_amd64.tar.gz
tar xzf "$tmp/gh.tgz" -C "$tmp"
install -m 0755 "$tmp/gh_2.63.2_linux_amd64/bin/gh" /usr/local/lib/gh-real
install -m 0755 "$here/gh-rest-shim.py" /usr/local/lib/gh-rest-shim.py
mkdir -p /home/linuxbrew/.linuxbrew/bin   # the path config/agents.yaml names for gh
ln -sf /usr/local/lib/gh-rest-shim.py /home/linuxbrew/.linuxbrew/bin/gh
ln -sf /usr/local/lib/gh-rest-shim.py /usr/local/bin/gh

# 2. The claude wrapper at the path config/agents.yaml names: IS_SANDBOX for root, no
#    auto-background in headless workers, planner/refiner refused.
mkdir -p /home/titan/.local/bin
install -m 0755 "$here/claude-wrapper.sh" /home/titan/.local/bin/claude

# 3. The planner the guard wakes after every run is refused too (its CLI comes from PATH).
install -m 0755 "$here/planner-disabled.sh" /usr/local/lib/planner-disabled
install -m 0755 "$here/sa-worker.sh" /usr/local/bin/sa-worker

# 4. agent-os's own interpreter needs Python 3.12 (the container's python3 is 3.11).
uv python install 3.12
rm -rf "$root/agent_os/.venv"
uv venv -p 3.12 "$root/agent_os/.venv"
uv pip install -p "$root/agent_os/.venv/bin/python" -e "$root/agent_os" "cryptography>=42"

# 5. Git identity for the human's commits (handoff rule), and the Claude worker worktree.
git -C "$root" config user.email 34090740+MatillaM@users.noreply.github.com
git -C "$root" config user.name MatillaM
"$root/scripts/worker_task.sh" claude init
(cd "$root/../studentassistant-claude/web" && npm ci --no-audit --no-fund)

echo "cloud shims ready: start workers with 'sa-worker claude branch|start|status|resume ...'"
