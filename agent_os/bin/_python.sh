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

# Where this package sits inside the host root `$1`, as a path relative to it: `agent_os` in a
# host that vendors the mechanism through `git subtree`, and NOTHING when the package IS the root
# (the mechanism's own repository) or lies outside it. Compared physically, because the host root
# comes from `git rev-parse` and this directory from a `cd`.
agent_os_relative_dir() {
  local dir root
  dir=$(cd "$agent_os_dir" 2>/dev/null && pwd -P) || return 0
  root=$(cd "$1" 2>/dev/null && pwd -P) || return 0
  case $dir in
    "$root"/*) printf '%s\n' "${dir#"$root"/}" ;;
  esac
  return 0
}

# The PYTHONPATH a run in worktree `$1` of host root `$2` is exported with (agent-os#35). The
# worktree's root always, which is what resolves a package of the host's own from the worktree.
# In a vendoring host, also the worktree's copy of this package's directory, AFTER the root: there
# `<worktree>/agent_os/` is the mechanism's repository and carries no `__init__.py`, so on the root
# alone it is only a namespace portion, and the regular package the venv's editable install puts
# on `sys.path` (the MAIN checkout's `agent_os/agent_os/`) wins over it -- measured with the
# mechanism's own interpreter, `python -c 'import agent_os; print(agent_os.__file__)'` run with
# `PYTHONPATH=<worktree>`. Named from the path alone, never from what the worktree holds, so the
# value is the same one `agent_run_environment_names` recomputes after the worktree is gone.
agent_os_worktree_pythonpath() {
  local relative
  relative=$(agent_os_relative_dir "$2")
  if [ -n "$relative" ]; then
    printf '%s:%s\n' "$1" "$1/$relative"
  else
    printf '%s\n' "$1"
  fi
}

# Links the host's `<package dir>/.venv` into worktree `$1` of host root `$2`, at the same relative
# path, when the package is vendored (agent-os#35): `git worktree add` brings tracked files only,
# and without it `agent_os/.venv/bin/pytest` -- the command that runs the mechanism's own suite --
# does not exist in the worktree. A link, never a copy, like the root `.venv` and `.env`. With
# `$3` = `only-if-ignored` the link is made only where git would ignore it, for a worktree that
# outlives the run: a branch cut before the mechanism's `.gitignore` named `.venv` without a slash
# would otherwise show the link as untracked, and an untracked entry refuses the next start.
agent_os_link_mechanism_venv() {
  local worktree=$1 root=$2 mode=${3-} relative
  relative=$(agent_os_relative_dir "$root")
  [ -n "$relative" ] || return 0
  [ -d "$root/$relative/.venv" ] && [ -d "$worktree/$relative" ] || return 0
  [ -e "$worktree/$relative/.venv" ] && return 0
  if [ "$mode" = only-if-ignored ]; then
    git -C "$worktree" check-ignore -q "$relative/.venv" 2>/dev/null || return 0
  fi
  ln -s "$root/$relative/.venv" "$worktree/$relative/.venv" && echo "linked $worktree/$relative/.venv -> $root/$relative/.venv"
}

# How the injected RULES blocks name the mechanism's own CLIs: `"$AGENT_OS_PYTHON" -m
# agent_os.<module>`, written literally in the prompt and never substituted. Every driver EXPORTS
# that variable, so the agent's own shell resolves it to the same interpreter the driver uses --
# and the prompt itself stays free of any absolute path, which would carry the HOST's directory
# name into the one text that must name no project (`agent_os/tests/test_agent_task.py`'s
# host-literal assertion).
