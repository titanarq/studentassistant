"""`agent-os doctor` — the first-run checklist of `agent_os/docs/AGENT_OS.md` §6, read back mechanically
instead of eyeballed once and forgotten.

Every check here READS state and nothing else: `gh auth status`, `gh label list`, `gh api`/`gh
project field-list` for the Project v2 board, file existence under the host's own tree, and
`systemctl --user is-active`. It never calls `agent_guard.py check` or any other trigger a role
reacts to -- a manual check would re-announce a run that already finished and wake the planner for
free (`agent_os/docs/AGENT_OS.md` §7, the `role_died`/exit-hook machinery in `agent_os.guard`) -- and it
never arms, restarts or edits a systemd unit: that stays a human decision
(`docs/runbooks/agent_monitor.md`).

    agent-os-doctor        # one line per check, exit 1 if any fails

Not a substitute for §4.3's GitHub steps (creating the Apps, the board, the labels) -- it only
reports which of them are still missing, the way `agent_os/docs/AGENT_OS.md`'s own "Not included" already
says a browser step never becomes code.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass

from agent_os.cli import host_root
from agent_os.issues import board_owner, gh_json, repo_name
from agent_os.lib import ProjectConfig, load_project

REQUIRED_GH_SCOPES = ("repo", "project")
BOARD_STATUS_FIELD = "Status"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"[{mark}] {self.name}: {self.detail}"


def check_python_version() -> Check:
    ok = sys.version_info >= (3, 12)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    detail = version if ok else f"{version} -- install python3.12 or newer"
    return Check("python3 >= 3.12", ok, detail)


def _gh_auth_scopes() -> tuple[bool, set[str], str]:
    """(logged in, scopes, raw failure message). `gh auth status` writes to stderr on most
    installs, so both streams are read -- the parser only cares about the one `Token scopes:` line
    either of them might carry."""
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return False, set(), (result.stderr or result.stdout).strip()
    scopes: set[str] = set()
    for line in (result.stdout + result.stderr).splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("- token scopes:"):
            raw = stripped.split(":", 1)[1]
            scopes = {item.strip().strip("'\"") for item in raw.split(",") if item.strip()}
    return True, scopes, ""


def check_gh_auth() -> Check:
    logged_in, scopes, failure = _gh_auth_scopes()
    if not logged_in:
        return Check(
            "gh auth status", False, f"not logged in -- {failure or 'run `gh auth login`'}"
        )
    missing = [scope for scope in REQUIRED_GH_SCOPES if scope not in scopes]
    if missing:
        return Check(
            "gh auth status",
            False,
            f"missing scope(s) {missing} (have {sorted(scopes)}) -- "
            f"`gh auth refresh -s {','.join(missing)}`",
        )
    return Check("gh auth status", True, f"scopes {sorted(scopes)}")


def check_labels(project: ProjectConfig, repo: str) -> Check:
    labels = project.labels
    required = [labels.ai_completed, labels.agents_paused, labels.auto_ready, labels.wake_planner]
    rows = gh_json("label", "list", "--repo", repo, "--limit", "200", "--json", "name") or []
    existing = {row["name"] for row in rows}
    missing = [name for name in required if name not in existing]
    if missing:
        create = "; ".join(f"gh label create '{name}' --repo {repo}" for name in missing)
        return Check("labels that do not autocreate", False, f"missing {missing} -- {create}")
    return Check("labels that do not autocreate", True, f"{required} all exist")


def check_board(project: ProjectConfig, repo: str) -> Check:
    owner = board_owner(repo)
    try:
        response = gh_json(
            "project", "field-list", str(project.board_number), "--owner", owner, "--format", "json"
        )
    except SystemExit as failure:
        return Check("Project v2 Status field", False, str(failure))
    fields = (response or {}).get("fields") or []
    single_selects = [field for field in fields if field.get("options") is not None]
    status = next(
        (field for field in single_selects if field.get("name") == BOARD_STATUS_FIELD), None
    )
    if status is None:
        return Check(
            "Project v2 Status field",
            False,
            f"project {owner}/{project.board_number} has no single-select field named "
            f"'{BOARD_STATUS_FIELD}'",
        )
    options = {option["name"] for option in status.get("options") or []}
    required = {value for value in project.board_columns.values() if value}
    missing = required - options
    if missing:
        return Check(
            "Project v2 Status field",
            False,
            f"'{BOARD_STATUS_FIELD}' field is missing option(s) {sorted(missing)}",
        )
    return Check("Project v2 Status field", True, f"'{BOARD_STATUS_FIELD}' has {sorted(required)}")


def check_app_secrets(project: ProjectConfig, root: pathlib.Path) -> Check:
    slugs = {
        project.planner_app,
        *(backend.app for backend in project.backends.values()),
        *project.role_apps.values(),
    }
    slugs.discard("")
    secrets_dir = root / project.secrets_dir
    missing = [
        slug
        for slug in sorted(slugs)
        if not (secrets_dir / f"{slug}.json").is_file()
        or not (secrets_dir / f"{slug}.pem").is_file()
    ]
    if missing:
        return Check(
            "GitHub App secrets",
            False,
            f"missing <slug>.json/<slug>.pem under {secrets_dir} for {missing}",
        )
    return Check("GitHub App secrets", True, f"{sorted(slugs)} present under {secrets_dir}")


def check_executables(project: ProjectConfig) -> Check:
    if not project.executables:
        return Check(
            "project.executables resolve", True, "none configured -- bare-name PATH lookup"
        )
    unresolved = []
    for name, value in project.executables.items():
        path = pathlib.Path(value)
        resolves = (
            path.is_file() and os.access(path, os.X_OK)
            if path.is_absolute()
            else shutil.which(value) is not None
        )
        if not resolves:
            unresolved.append(name)
    if unresolved:
        return Check("project.executables resolve", False, f"do not resolve: {unresolved}")
    return Check("project.executables resolve", True, f"{sorted(project.executables)} all resolve")


def check_worktrees(project: ProjectConfig, root: pathlib.Path) -> Check:
    worktrees = {name: b.worktree for name, b in project.backends.items() if b.worktree}
    if not worktrees:
        return Check("worktrees exist", True, "none configured")
    missing = [
        backend
        for backend, relative in worktrees.items()
        if not (root / relative / ".git").exists()
    ]
    if missing:
        return Check(
            "worktrees exist",
            False,
            f"no worktree for {missing} -- `worker_task.sh <backend> init`",
        )
    return Check("worktrees exist", True, f"{sorted(worktrees)} all exist")


def check_notify_topic(project: ProjectConfig, root: pathlib.Path) -> Check:
    path = root / project.notify_topic_file
    if path.is_file():
        return Check("notify topic file", True, str(path))
    return Check(
        "notify topic file", False, f"{path} missing -- write the ntfy topic string into it"
    )


def check_guard_timer(project: ProjectConfig) -> Check:
    if not project.guard_unit:
        return Check("guard timer active", False, "project.guard_unit is not set")
    unit = f"{project.guard_unit}.timer"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, check=False
    )
    state = result.stdout.strip() or "not installed"
    ok = state == "active"
    detail = state if ok else f"{state} -- see docs/runbooks/agent_monitor.md's Install section"
    return Check("guard timer active", ok, detail)


def run_checks(project: ProjectConfig, root: pathlib.Path, repo: str) -> list[Check]:
    return [
        check_python_version(),
        check_gh_auth(),
        check_labels(project, repo),
        check_board(project, repo),
        check_app_secrets(project, root),
        check_executables(project),
        check_worktrees(project, root),
        check_notify_topic(project, root),
        check_guard_timer(project),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    root = host_root()
    project = load_project()
    repo = repo_name()

    checks = run_checks(project, root, repo)
    for check in checks:
        print(check.line())

    if any(not check.ok for check in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
