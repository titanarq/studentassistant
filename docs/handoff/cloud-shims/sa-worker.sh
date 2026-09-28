#!/bin/sh
# Cloud wrapper for agent-os worker runs: the planner never runs on its own here.
export PLANNER_CLAUDE_BIN=/usr/local/lib/planner-disabled PLANNER_QWEN_BIN=/usr/local/lib/planner-disabled
exec /home/user/studentassistant/scripts/worker_task.sh "$@"
