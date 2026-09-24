#!/usr/bin/env bash
# Compatibility wrapper: the driver is `agent_os/bin/worker_task.sh`, which runs either backend.
exec "$(dirname "${BASH_SOURCE[0]}")/worker_task.sh" qwen "$@"
