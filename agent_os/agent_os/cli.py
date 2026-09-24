"""Where the mechanism answers two questions for itself: which interpreter am I, and which project
am I running?

The shell half of exactly the same pair lives in `agent_os/bin/_python.sh` (`agent_os_python`,
`agent_os_host_root`); this is the Python half, for the entry points that shell out to a driver or
to another of the package's own modules. There is one rule each and no second copy of either.

Until 2026-09-21 both answers were the same accident: every call site spelled the interpreter
inside the HOST project's own root virtualenv, and every path was derived from `__file__`'s
grandparent, which was the host root only because the mechanism's files lived in the host's
`scripts/`. Neither survives the move (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-
by-hosts-and-never-modified.md).
"""

from __future__ import annotations

import os
import pathlib
import subprocess

# `agent_os/agent_os/cli.py` -> `agent_os/`: the package directory, the one `bootstrap.sh` builds
# a virtualenv beside and `bin/_python.sh` resolves from its own location.
AGENT_OS_DIR = pathlib.Path(__file__).resolve().parents[1]


def agent_os_python() -> str:
    """The interpreter that runs this package: `$AGENT_OS_PYTHON`, else the virtualenv beside the
    package, else `python3`.

    The same three steps as `agent_os_python()` in `agent_os/bin/_python.sh`, in the same order, so
    a driver and a Python entry point that shell out to each other cannot end up on two different
    interpreters. The last step is a fallback, not a promise: a `python3` without the declared
    dependencies fails on the import rather than half-working."""
    configured = os.environ.get("AGENT_OS_PYTHON")
    if configured:
        return configured
    candidate = AGENT_OS_DIR / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return "python3"


def host_root() -> pathlib.Path:
    """The HOST project's root: `$AGENT_OS_HOST_ROOT`, else the git checkout the cwd belongs to,
    else the directory above this package.

    Everything the mechanism keeps for a project hangs off this one path -- `config/agents.yaml`,
    `.cache/`, `.secrets/`, `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries -- and NONE
    of it is resolved against this package's own location any more, which is what made the
    mechanism only ever able to run the project it was vendored into.

    Every shell driver EXPORTS the root it resolved, so in a real run the environment variable
    answers first and the git call never happens: a process standing in a worker's worktree is
    handed the checkout whose driver launched it instead of reading its own tree."""
    configured = os.environ.get("AGENT_OS_HOST_ROOT")
    if configured:
        return pathlib.Path(configured).resolve()
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        toplevel = ""
    if toplevel:
        return pathlib.Path(toplevel).resolve()
    return AGENT_OS_DIR.parent
