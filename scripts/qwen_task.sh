#!/usr/bin/env bash
# Shim: this driver lives in `agent_os/bin/qwen_task.sh` (`agent_os/docs/adr/2026-09-21-the-
# mechanism-is-one-directory-extended-by-hosts-and-never-modified.md`). The path is kept because
# the systemd unit, the `.claude/agents/*.md` prompts, `CLAUDE.md`, `docs/modules/workers.md` and
# the memory notes all name it, and because the worker worktrees execute the MAIN checkout's copy
# -- a rename with no shim breaks a run in flight. It adds no behaviour and no argument of its own.
# Workaround titanarq/agent-os#27 (docs/runbooks/operations.md): the one thing it does add is a
# default AGENT_OS_PYTHON of scripts/agent_os_patches/python, the interpreter wrapper that runs
# `-m agent_os.issues` with cheap board lookups; the driver exports it to every child. Drop the
# export once agent-os#27 is fixed and the subtree is pulled.
here=$(dirname "$(realpath "${BASH_SOURCE[0]}")")
export AGENT_OS_PYTHON=${AGENT_OS_PYTHON:-$here/agent_os_patches/python}
exec "$here/../agent_os/bin/qwen_task.sh" "$@"
