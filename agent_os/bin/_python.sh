#!/usr/bin/env bash
# The three paths every driver in this directory needs, resolved in ONE place: the package's own
# directory, the interpreter to run it with, and the HOST project's root.
#
# SOURCED, never executed. Sourcing it twice is harmless (it only defines functions and one
# variable), which is what lets `worker_task.sh` source `agent_task.sh`, which sources this.
#
# Why a resolver rather than a literal interpreter path: until 2026-09-21 every call site spelled
# the interpreter inside the HOST project's own root virtualenv, so the mechanism only ran inside a
# host that happened to have one, carrying `pyyaml` and `pydantic` -- true of the first host, false
# of a second one with no root virtualenv at all. The mechanism carries its own interpreter now
# (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-never-modified.md).

# The directory this file's own `bin/` lives in, i.e. the package root.
agent_os_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# `$AGENT_OS_PYTHON` if the caller set one (a host with its own installation, or a test), else the
# virtualenv `bootstrap.sh` builds beside this directory, else whatever `python3` the PATH offers.
# The last step is a fallback, not a promise: a `python3` without the declared dependencies fails
# on the import, loudly, which is the right answer to "the mechanism was never bootstrapped".
agent_os_python() {
  if [ -n "${AGENT_OS_PYTHON:-}" ]; then
    printf '%s\n' "$AGENT_OS_PYTHON"
    return 0
  fi
  local venv=$agent_os_dir/.venv
  if [ -x "$venv/bin/python" ]; then
    printf '%s\n' "$venv/bin/python"
    return 0
  fi
  printf '%s\n' python3
}

# The HOST project's root -- where `config/agents.yaml`, `.cache/`, `.secrets/` and the worktree
# paths are resolved from. `$AGENT_OS_HOST_ROOT` wins, then the git checkout this package sits in,
# then the directory above it. NEVER the package's own location alone.
#
# The toplevel of the tree the package sits in, not of the cwd: a role's own process has a linked
# worktree for a cwd and must still read the checkout whose driver is running. Every driver EXPORTS
# the value it resolves, so a child process -- a worker's subshell, the guard's exit hook -- is
# handed the answer instead of resolving it again from wherever it happens to stand.
agent_os_host_root() {
  if [ -n "${AGENT_OS_HOST_ROOT:-}" ]; then
    printf '%s\n' "$AGENT_OS_HOST_ROOT"
    return 0
  fi
  git -C "$agent_os_dir" rev-parse --show-toplevel 2>/dev/null && return 0
  dirname "$agent_os_dir"
}

# How the injected RULES blocks name the mechanism's own CLIs: `"$AGENT_OS_PYTHON" -m
# agent_os.<module>`, written literally in the prompt and never substituted. Every driver EXPORTS
# that variable, so the agent's own shell resolves it to the same interpreter the driver uses --
# and the prompt itself stays free of any absolute path, which would carry the HOST's directory
# name into the one text that must name no project (`agent_os/tests/test_agent_task.py`'s
# host-literal assertion).
