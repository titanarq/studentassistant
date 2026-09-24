#!/usr/bin/env bash
# The project's single test command (config/agents.yaml project.test_command).
# Runs every suite whose skeleton exists and prints a compact summary per suite; full output in
# .cache/test-<suite>-last.log -- grep that instead of re-running.
# Usage: scripts/test.sh                      all suites
#        scripts/test.sh backend [pytest args]   e.g. scripts/test.sh backend -k vault
#        scripts/test.sh web [vitest args]
#        scripts/test.sh android [gradle args]   e.g. scripts/test.sh android :app:testDebugUnitTest
# Never runs integration tests (GPU, network, real Claude) nor instrumented Android tests.
# Self-provisioning: on a fresh checkout or worktree (e.g. the validator's throwaway worktree,
# which gets no environment from the agent-os driver) it creates what each suite needs from the
# committed lock files -- backend/.venv via `uv sync --frozen`, web/node_modules via `npm ci` --
# so running this command IS the supported way to get an environment; nothing is linked from
# another tree. Only the first run needs the network (package download).
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache
max_lines=${TEST_MAX_LINES:-120}

report() { # suite status log
  if [ "$2" -eq 0 ]; then
    echo "OK: $1 -- full log: $3"
  else
    echo "FAILED (exit $2): $1 -- full log: $3"
    grep -nE 'FAILED|ERROR|Error|error:|e: |What went wrong|Exception|✗|×' "$3" | head -n "$max_lines"
  fi
}

run_backend() {
  local log=.cache/test-backend-last.log
  if [ ! -f backend/pyproject.toml ]; then echo "skip backend: no backend/pyproject.toml yet"; return 0; fi
  ( cd backend && { [ -x .venv/bin/python ] || { echo "bootstrap: uv sync --frozen (no backend/.venv)"; uv sync --frozen; }; } \
      && uv run --frozen ruff check . && uv run --frozen ruff format --check . \
      && uv run --frozen pytest -q -m "not integration" "$@" ) >"$log" 2>&1
  local s=$?; report backend $s "$log"; return $s
}

run_web() {
  local log=.cache/test-web-last.log
  if [ ! -f web/package.json ]; then echo "skip web: no web/package.json yet"; return 0; fi
  ( cd web && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm test -- --run "$@" ) >"$log" 2>&1
  local s=$?; report web $s "$log"; return $s
}

run_android() {
  local log=.cache/test-android-last.log
  if [ ! -x android/gradlew ]; then echo "skip android: no android/gradlew yet"; return 0; fi
  [ "$#" -eq 0 ] && set -- test
  ( cd android && ./gradlew --console=plain --no-daemon "$@" ) >"$log" 2>&1
  local s=$?; report android $s "$log"; return $s
}

case "${1:-all}" in
  backend) shift; run_backend "$@" ;;
  web) shift; run_web "$@" ;;
  android) shift; run_android "$@" ;;
  all)
    status=0
    run_backend || status=1
    run_web || status=1
    run_android || status=1
    exit $status ;;
  *) echo "usage: $0 [backend|web|android|all] [args]"; exit 2 ;;
esac
