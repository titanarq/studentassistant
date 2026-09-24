#!/usr/bin/env python3
"""Shim: this module lives in `agent_os/agent_os/lib.py` (`agent_os/docs/adr/2026-09-21-the-
mechanism-is-one-directory-extended-by-hosts-and-never-modified.md`). The path is kept because the
systemd unit, the `.claude/agents/*.md` prompts, `CLAUDE.md`, `docs/modules/workers.md` and the
memory notes all name it, and because the worker worktrees execute the MAIN checkout's copy -- a
rename with no shim breaks a run in flight.

It re-executes the mechanism on ITS OWN interpreter and adds no behaviour and no argument of its
own. That interpreter is what `bash agent_os/bootstrap.sh` builds: without it the resolver falls
back to `python3`, which fails on the import unless that one happens to carry the mechanism's
dependencies -- so the bootstrap is a prerequisite of this path, not an optimisation."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "agent_os")
)
from agent_os.cli import agent_os_python

os.execvp(agent_os_python(), [agent_os_python(), "-m", "agent_os.lib", *sys.argv[1:]])
