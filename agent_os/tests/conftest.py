"""The mechanism's own fixtures, and nothing of any host project.

It is a separate file from the host's `tests/conftest.py` on purpose (#508): that one imports the
host's database layer at module scope, which a suite meant to run in a project with no database
cannot do. Nothing here touches a network, a database or a real backend, so
`agent_os/.venv/bin/pytest agent_os/tests -q` is the whole run.

The helper below is imported by name (`from conftest import config_with_never_run`): pytest puts
this directory on `sys.path` for its own collection, so the module is reachable as `conftest`.

`AGENTS_CONFIG_PATH` is set here, before anything imports `agent_os.lib`, so the whole suite loads
`config.example.yaml` rather than a host's real `config/agents.yaml` (#512). Without this, a bare
`load_project()`/`load_mechanism()`/`load_planner_config()` call -- and there are dozens, in this
suite and in the mechanism's own modules (`agent_os.guard`'s `PROJECT = load_project()` at import
time among them) -- resolves `DEFAULT_AGENTS_CONFIG` (`agent_os/agent_os/lib.py`) against
`HOST_ROOT/config/agents.yaml`, and `HOST_ROOT` finds a real project's root through `git
rev-parse --show-toplevel` regardless of where `agent_os/` sits inside it. Run this suite from
inside a checkout that carries a `config/agents.yaml` of its own -- exactly the layout `agent_os/
tests -q` runs under today, vendored in a host project -- and every one of those bare calls
silently reads that host's file instead of the example one, which is what let host-specific
assertions accumulate in this suite in the first place. `DEFAULT_AGENTS_CONFIG` binds ONCE, at the first
import of `agent_os.lib` in the whole process, so this has to run before that import happens
anywhere -- which for pytest means here, in the `conftest.py` loaded before any test module in
this directory -- and `setdefault` rather than a plain assignment, so a caller who deliberately
exports `AGENTS_CONFIG_PATH` before invoking pytest (a fixture-built config of its own) is not
overridden.
"""

from __future__ import annotations

import os
import pathlib
import re

import yaml

_THIS_PACKAGE = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENTS_CONFIG_PATH", str(_THIS_PACKAGE / "config.example.yaml"))

from agent_os.cli import AGENT_OS_DIR

# The HOST project this helper used to patch a copy of: since #512 the source is the mechanism's
# own shipped example, not a host's real file, so this fixture behaves identically whichever
# project's `agent_os/` it runs inside.
EXAMPLE_CONFIG = AGENT_OS_DIR / "config.example.yaml"


def config_with_never_run(tmp_path, items):
    """A copy of `config.example.yaml` with `project.never_run` replaced by `items` -- (command,
    reason) pairs, or nothing at all for an empty list -- written under `tmp_path` and returned as
    a path. The drivers pick it up through `AGENTS_CONFIG_PATH`, which is how a test renders the
    three RULES blocks from a list this repository does not ship, and from an empty one
    (`agent_os/docs/AGENT_OS.md` §7 row (b), issue #363). Shared by `test_worker_task.py` and
    `test_agent_task.py` because the point of the list is that all three blocks read it."""
    text = EXAMPLE_CONFIG.read_text()
    block = (
        "  never_run: []\n"
        if not items
        else "  never_run:\n"
        + "".join(
            f'    - command: "{command}"\n      reason: "{reason}"\n' for command, reason in items
        )
    )
    patched, count = re.subn(
        r"^  never_run:\n(?:    .*\n)+",
        # A callable rather than the string itself: `re.subn` would read a backslash inside a
        # reason as one of its own escapes.
        lambda _match: block,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "config.example.yaml's project.never_run block changed shape"
    path = pathlib.Path(tmp_path) / "agents.yaml"
    path.write_text(patched)
    return path


def config_with_no_host_text(tmp_path, name="agents-without-host-text.yaml"):
    """A copy of `config.example.yaml` with every key a host fills PROMPT TEXT from emptied: the
    commands it forbids, the two path lists, the environment it exports, and the extra-prompt
    files it owns. What a role's rendered prompt still says under this config is the mechanism's
    own contract and nothing else, which is what the literal tests measure (#363, #509).

    A yaml round-trip rather than a regex over the text: five keys of four different shapes, and
    the file's comments are of no use to a fixture."""
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["project"]["never_run"] = []
    data["project"]["forbidden_paths"] = []
    data["project"]["merge_audit_exempt_paths"] = []
    data["project"]["worker_environment"] = {}
    data["project"]["prompt_extras"] = {}
    data["mechanism"]["own_paths"] = []
    path = pathlib.Path(tmp_path) / name
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path
