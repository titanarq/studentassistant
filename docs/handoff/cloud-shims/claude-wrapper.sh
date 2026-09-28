#!/bin/sh
# Cloud shim (not in the repo): root container needs IS_SANDBOX for headless runs; the
# autonomous planner/refiner are disabled because the cloud orchestrator plans by hand.
case "${AGENT_RUN_ROLE:-}" in
  planner|refiner)
    echo "cloud shim: role '$AGENT_RUN_ROLE' disabled in the cloud session; the orchestrator plans by hand" >&2
    exit 1 ;;
esac
# A headless worker must run its commands in the foreground: with the orchestrator session's
# auto-background setting, `-p` ends the turn while a test run is still going.
unset CLAUDE_AUTO_BACKGROUND_TASKS CLAUDE_CODE_BG_TASKS_REPORT_RUNNING
export IS_SANDBOX=1
exec /opt/node22/bin/claude "$@"
