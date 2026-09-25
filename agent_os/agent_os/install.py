"""Adopting the mechanism on a machine, from `config/agents.yaml` alone.

`agent-os install [--dry-run] [--force]` writes the systemd `--user` units the tick runs on, and
copies the host's `.claude/agents/*.md`, `.github/ISSUE_TEMPLATE/*.md`, the mechanism's CI snippet
and a host CI workflow running `project.test_command` on every pull request if they are absent -- the three files `agent_os/docs/AGENT_OS.md` §5 step 7 used to say were "machine steps, not code,
but manual regardless" (`agent_os/docs/AGENT_OS.md` §7 row (h)). It never enables, restarts or reloads a
systemd unit: arming the timer stays a human decision
(`agent_os/docs/adr/2026-09-14-the-monitor-and-planner-run-on-triggers-never-as-a-standing-process.md`,
`agent_os/docs/ADOPTION.md` step 22).

    agent-os-install --dry-run     # print every path this would touch and its diff, write nothing
    agent-os-install               # write what does not already exist
    agent-os-install --force       # also overwrite a file that already exists and differs

Every path is either created or left alone: an existing file that already matches the rendered
content is reported and skipped, one that differs is reported with its diff and left alone unless
`--force` says to overwrite it. Nothing here is destructive on its own -- the worst `--force` does
is overwrite a file this same command would write again identically after `--force`.
"""

from __future__ import annotations

import argparse
import difflib
import os
import pathlib
import re
import sys

from agent_os.cli import AGENT_OS_DIR, agent_os_python, host_root
from agent_os.lib import CONFIG_LOAD_ERRORS, ProjectConfig, config_load_failure, load_project
from agent_os.render import render_agent_template

SYSTEMD_TEMPLATES_DIR = AGENT_OS_DIR / "templates" / "systemd"
ISSUE_TEMPLATES_DIR = AGENT_OS_DIR / "templates" / "issue_template"
CI_SNIPPET_SOURCE = AGENT_OS_DIR / "templates" / "ci-agent-os.yml"
HOST_CI_WORKFLOW_SOURCE = AGENT_OS_DIR / "templates" / "ci-host.yml"
HOST_CI_WORKFLOW_NAME = "ci-host.yml"
AGENT_TEMPLATES_DIR = AGENT_OS_DIR / "agents"

# The standard systemd/POSIX default -- present on every Linux box regardless of what this one
# happens to have installed under `~`, and the same tail the units armed by hand on this machine
# already end with. Never a host literal: nothing under it names a person's home directory.
DEFAULT_PATH_TAIL = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UNKNOWN_TOKEN_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


class InstallError(Exception):
    """A precondition install cannot proceed without: an unconfigured key the caller must set in
    `config/agents.yaml` before running this again."""


def _require_guard_unit(project: ProjectConfig) -> str:
    if not project.guard_unit:
        raise InstallError(
            "project.guard_unit is not set in config/agents.yaml -- name the systemd unit "
            "before running install (agent_os/docs/AGENT_OS.md §4.2)"
        )
    return project.guard_unit


def executables_path_prefix(project: ProjectConfig) -> str:
    """One directory per configured `project.executables` entry, in declaration order and without
    repeats, ahead of the standard tail. Empty when the project configures no absolute path, which
    is exactly what `project.executables: {}` means everywhere else in the mechanism -- the tick
    then resolves `gh`, `git` and any bare-name backend off whatever PATH the caller already has."""
    directories: list[str] = []
    for value in project.executables.values():
        directory = str(pathlib.Path(value).parent)
        if directory not in directories:
            directories.append(directory)
    return ":".join([*directories, DEFAULT_PATH_TAIL])


def unit_python() -> str:
    """The mechanism's own interpreter as `agent_os_python()` resolves it, required to be an
    absolute path to an executable file -- because a systemd unit runs it with whatever PATH the
    `--user` manager has, which nothing guarantees carries a `python3` at all, let alone one with
    the mechanism's dependencies (#12).

    `agent_os_python()`'s last step, the bare `python3`, is a fallback a shell driver can afford:
    it fails on the import, in front of whoever ran it. Frozen into a unit it fails five minutes
    later on a timer nobody is watching, so install refuses it instead."""
    python = agent_os_python()
    if not os.path.isabs(python) or not (os.path.isfile(python) and os.access(python, os.X_OK)):
        raise InstallError(
            f"no absolute interpreter for the guard unit's ExecStart (resolved {python!r}) -- "
            "run `bash agent_os/bootstrap.sh` to build the mechanism's own .venv, or set "
            "AGENT_OS_PYTHON to the absolute path of an interpreter that imports agent_os"
        )
    return python


def resolve_exec_start(root: pathlib.Path) -> str:
    """The host's own shim (`scripts/agent_guard.py`) when one exists -- the path the host's
    prompts, docs and hand-armed units already name -- or the package's own module form otherwise,
    which is what a host with no shims (one that never ran #508's move) gets instead.

    Both run on `unit_python()`, the mechanism's own interpreter, and never on a host's: a host
    root `.venv` carries the host's package versions, which must not decide how the guard behaves
    (AGENT_OS.md §8, #51). The shim itself only re-executes `agent_os.guard` on that same
    interpreter, so it needs nothing the host's venv provides."""
    shim = root / "scripts" / "agent_guard.py"
    if shim.is_file():
        return f"{unit_python()} {shim} tick"
    return f"{unit_python()} -m agent_os.guard tick"


def _refuse_unknown_tokens(rendered: str, *, source: str) -> None:
    leftover = UNKNOWN_TOKEN_RE.findall(rendered)
    if leftover:
        raise InstallError(f"{source}: unknown token(s) left in the rendered output: {leftover}")


def render_systemd_unit(name: str, project: ProjectConfig, root: pathlib.Path) -> str:
    """`name` is `guard.service`, `guard.timer` or `override.conf` -- the templates under
    `agent_os/templates/systemd/<name>.tmpl`, unrelated to the generic `__TOKEN__` renderer stage 3
    uses for the agent prompts: these tokens are internal to this module, which owns every
    template that defines them."""
    template = (SYSTEMD_TEMPLATES_DIR / f"{name}.tmpl").read_text()
    guard_unit = _require_guard_unit(project)
    path_value = executables_path_prefix(project)
    descriptions = {
        "guard.service": f"{guard_unit} tick (budget/liveness/quota check)",
        "guard.timer": f"Run the {guard_unit} tick on a schedule",
        "override.conf": "",
    }
    path_source_notes = {
        "override.conf": (
            "every directory in project.executables, ahead of the standard tail"
            if project.executables
            else "just the standard tail -- project.executables configures no absolute path today"
        ),
    }
    substitutions = {
        "__GUARD_UNIT__": guard_unit,
        "__WORKING_DIRECTORY__": str(root),
        "__EXEC_START__": resolve_exec_start(root),
        "__PATH__": path_value,
        "__DESCRIPTION__": descriptions.get(name, ""),
        "__PATH_SOURCE_NOTE__": path_source_notes.get(name, ""),
    }
    rendered = template
    for token, value in substitutions.items():
        rendered = rendered.replace(token, value)
    _refuse_unknown_tokens(rendered, source=f"templates/systemd/{name}.tmpl")
    return rendered


class Action:
    """One file this run considered: where it would go, what it would contain, and whether that
    differs from what is there today. `write()` is the only thing that touches disk, and only
    `main()` decides whether to call it."""

    def __init__(self, dest: pathlib.Path, content: str):
        self.dest = dest
        self.content = content
        self.existed = dest.exists()
        self.old_content = dest.read_text() if self.existed else None
        self.unchanged = self.existed and self.old_content == content

    @property
    def diff(self) -> str:
        if not self.existed:
            return ""
        return "".join(
            difflib.unified_diff(
                (self.old_content or "").splitlines(keepends=True),
                self.content.splitlines(keepends=True),
                fromfile=f"{self.dest} (current)",
                tofile=f"{self.dest} (generated)",
            )
        )

    def status(self, *, force: bool, dry_run: bool) -> str:
        """What happened to this file -- or, under `dry_run`, what would have. Only a dry run
        speaks in the conditional: `main()` prints this after `write()`, so on a real run the
        past tense is already true when the line appears (agent-os#9)."""
        if not self.existed:
            verb = "would create" if dry_run else "created"
            return verb if self.content else f"{verb} (empty)"
        if self.unchanged:
            return "exists, up to date -- skipped"
        if force:
            verb = "would overwrite" if dry_run else "overwritten"
            return f"exists and differs -- {verb} (--force)"
        return "exists and differs -- refusing without --force"

    def should_write(self, *, force: bool) -> bool:
        return not self.existed or (not self.unchanged and force)

    def write(self) -> None:
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.dest.write_text(self.content)


def plan_systemd_units(project: ProjectConfig, root: pathlib.Path, systemd_user_dir: pathlib.Path):
    guard_unit = _require_guard_unit(project)
    service = Action(
        systemd_user_dir / f"{guard_unit}.service",
        render_systemd_unit("guard.service", project, root),
    )
    timer = Action(
        systemd_user_dir / f"{guard_unit}.timer",
        render_systemd_unit("guard.timer", project, root),
    )
    override = Action(
        systemd_user_dir / f"{guard_unit}.service.d" / "override.conf",
        render_systemd_unit("override.conf", project, root),
    )
    return [service, timer, override]


def plan_issue_templates(root: pathlib.Path) -> list[Action]:
    if not ISSUE_TEMPLATES_DIR.is_dir():
        return []
    actions = []
    for name in ("task.md", "bug.md"):
        source = ISSUE_TEMPLATES_DIR / name
        if not source.is_file():
            continue
        actions.append(Action(root / ".github" / "ISSUE_TEMPLATE" / name, source.read_text()))
    return actions


def plan_ci_snippet(root: pathlib.Path) -> list[Action]:
    if not CI_SNIPPET_SOURCE.is_file():
        return []
    return [
        Action(root / ".github" / "workflows" / "ci-agent-os.yml", CI_SNIPPET_SOURCE.read_text())
    ]


def plan_host_ci_workflow(project: ProjectConfig, root: pathlib.Path) -> list[Action]:
    """`.github/workflows/ci-host.yml`, running `project.test_command` on every pull request with
    no path filter, so no PR reaches the control plane with zero checks -- which its merge
    condition 1 counts as not met (agent-os#50). `ci-agent-os.yml` alone only fires on
    `agent_os/**`. A host whose own CI already reports on every PR opts out with
    `project.install_host_ci: false`."""
    if not project.install_host_ci:
        return []
    rendered = render_agent_template(HOST_CI_WORKFLOW_SOURCE.read_text(), project)
    return [Action(root / ".github" / "workflows" / HOST_CI_WORKFLOW_NAME, rendered)]


def plan_agent_templates(
    project: ProjectConfig, root: pathlib.Path
) -> tuple[list[Action], str | None]:
    """The `.claude/agents/{control-plane,worker-runner}.md` prompts, rendered from
    `agent_os/agents/*.md` by the generic `__TOKEN__` renderer (`agent_os.render`). `agent_os/agents/`
    is #510's own deliverable, landing in parallel -- absent here, this reports "no templates dir,
    skipped" instead of failing, exactly as the issue asks."""
    if not AGENT_TEMPLATES_DIR.is_dir():
        return [], "no templates dir (agent_os/agents/), skipped"
    actions = []
    for name in ("control-plane.md", "worker-runner.md"):
        source = AGENT_TEMPLATES_DIR / name
        if not source.is_file():
            continue
        rendered = render_agent_template(source.read_text(), project)
        actions.append(Action(root / ".claude" / "agents" / name, rendered))
    return actions, None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would happen, write nothing"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite a file that already differs"
    )
    args = parser.parse_args()

    root = host_root()
    try:
        project = load_project()
    except CONFIG_LOAD_ERRORS as error:
        sys.exit(config_load_failure(error))
    systemd_user_dir = pathlib.Path.home() / ".config" / "systemd" / "user"

    try:
        actions = plan_systemd_units(project, root, systemd_user_dir)
    except InstallError as error:
        sys.exit(str(error))

    agent_actions, agent_note = plan_agent_templates(project, root)
    actions += agent_actions
    actions += plan_issue_templates(root)
    actions += plan_ci_snippet(root)
    actions += plan_host_ci_workflow(project, root)

    failed = False
    for action in actions:
        if not args.dry_run and action.should_write(force=args.force):
            action.write()
        print(f"{action.dest}: {action.status(force=args.force, dry_run=args.dry_run)}")
        if action.existed and not action.unchanged:
            print(action.diff)
        if action.existed and not action.unchanged and not args.force:
            failed = True

    if agent_note:
        print(agent_note)

    if failed:
        sys.exit(
            "one or more files already exist with different content -- rerun with --force to "
            "overwrite them, or resolve the difference by hand"
        )


if __name__ == "__main__":
    main()
