#!/usr/bin/env bash
# Host wrapper around `agent_os/bootstrap.sh` (ADOPTION.md step 16/25). Run this instead of the
# bare bootstrap, after every `git subtree pull`.
# Workaround: agent_os/pyproject.toml declares `PyJWT` but not `cryptography`, so the mechanism's
# own interpreter cannot sign the RS256 App JWT and `gh_app_token` fails with KeyError 'RS256'.
# Remove the extra install once agent_os depends on `PyJWT[crypto]` itself.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash "$root/agent_os/bootstrap.sh"
"$root/agent_os/.venv/bin/python" -m pip install --quiet "cryptography>=42"
"$root/agent_os/.venv/bin/python" -c 'import jwt.algorithms as a; assert a.has_crypto; print("agent_os: RS256 available")'
