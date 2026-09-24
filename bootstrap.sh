#!/usr/bin/env bash
# Creates the mechanism's OWN interpreter and installs the package into it, editable.
#
#   bash agent_os/bootstrap.sh
#
# Idempotent: a second run reinstalls into the same virtualenv. Nothing here touches the host
# project's interpreter, and nothing in the mechanism needs the host's one to exist -- that split
# is the whole point (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-
# never-modified.md). `AGENT_OS_BOOTSTRAP_PYTHON` picks the base interpreter to build it with.
#
# The dev dependencies are named here rather than installed as a group: they are declared as a PEP
# 735 `[dependency-groups]`, not as an extra, so `-e ".[dev]"` would fail on "no such extra".
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
venv=$here/.venv
base=${AGENT_OS_BOOTSTRAP_PYTHON:-python3}

[ -x "$venv/bin/python" ] || "$base" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
"$venv/bin/python" -m pip install --quiet -e "$here" pytest ruff

echo "agent_os interpreter: $venv/bin/python"
"$venv/bin/python" -c 'import agent_os; print("agent_os:", agent_os.__file__)'
