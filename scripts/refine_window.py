#!/usr/bin/env python3
"""Keep the refiner's queue short and in critical-path order.

Host-side workaround for titanarq/agent-os#32 (see docs/runbooks/operations.md,
"Refine window"): `agent_os.guard.refinable_issues` returns every `status:refine` issue that still
needs refining in `gh issue list` order (newest first), `refine_pending` names only the first ten,
and the planner refines one per wake -- so with a 70-task backlog the refiner starts on the
highest-numbered, lowest-priority tasks and the critical path (#14, #15, ...) is refined last, days
later. Nothing ranks the backlog by priority or by whether the parent epic carries `auto-ready`.

This script keeps at most WINDOW issues that still need refining under `status:refine`; the rest
are parked (label `parked`, no `status:*` label, so every part of the mechanism ignores them and the
board sync shows them in Backlog). Rank: parent epic has `auto-ready` first, then priority label
(p1 < p2 < ...), then issue number. With the window at most ten, `refine_pending` names all of it.

Run it with the mechanism's own interpreter, which carries agent_os.lib:
  agent_os/.venv/bin/python scripts/refine_window.py <mode>

  refine_window.py park [--window N]   park every refine-needing issue outside the top N (once)
  refine_window.py tick [--window N]   unpark the best parked issues until the window is full
                                       (the board-sync timer runs this; silent when nothing to do)
  refine_window.py unpark-all          put every parked issue back under status:refine
  add --dry-run to any of them to print without changing anything.

Only issues that still need refining are ever parked; a refined issue waiting for its blockers or
for `auto-ready` stays where it is. Issues the refiner creates while splitting are never parked
(only `park` parks, and it is run by hand). REST only (`gh api`), never GraphQL. Remove this script,
its ExecStart line and the runbook section once agent-os#32 is fixed and the subtree pulled.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from agent_os.lib import budget_failures, label_names, load_project, load_task_classes, section_failures

ROOT = Path(__file__).resolve().parent.parent
PARKED = "parked"
DEFAULT_WINDOW = 3


def gh(*args: str, method: str = "GET", fields: list[str] | None = None) -> object:
    cmd = [GH, "api", "-X", method, *args]
    for field in fields or []:
        cmd += ["-f", field]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def open_issues(repo: str) -> list[dict]:
    rows, page = [], 1
    while True:
        batch = gh(f"repos/{repo}/issues?state=open&per_page=100&page={page}")
        rows += [row for row in batch if "pull_request" not in row]
        if len(batch) < 100:
            return rows
        page += 1


def needs_refining(issue: dict, classes: dict) -> bool:
    body = issue.get("body") or ""
    return bool(section_failures(body) + budget_failures(body, classes))


def parent_labels(repo: str, issues: list[dict]) -> dict[int, set[str]]:
    """issue number -> its parent's labels, via each open epic's sub-issue list."""
    result: dict[int, set[str]] = {}
    for epic in issues:
        if "type:epic" not in label_names(epic):
            continue
        for child in gh(f"repos/{repo}/issues/{epic['number']}/sub_issues?per_page=100"):
            result[child["number"]] = label_names(epic)
    return result


def rank(issue: dict, parents: dict[int, set[str]], auto_ready: str) -> tuple[int, int, int]:
    prio = min((int(m.group(1)) for name in label_names(issue) if (m := re.fullmatch(r"p(\d)", name))), default=9)
    return (0 if auto_ready in parents.get(issue["number"], set()) else 1, prio, issue["number"])


def relabel(repo: str, number: int, add: str, remove: str, dry_run: bool) -> None:
    print(f"#{number}: -{remove} +{add}")
    if dry_run:
        return
    gh(f"repos/{repo}/issues/{number}/labels", method="POST", fields=[f"labels[]={add}"])
    try:
        gh(f"repos/{repo}/issues/{number}/labels/{remove}", method="DELETE")
    except RuntimeError as error:
        print(f"#{number}: could not remove {remove}: {error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["park", "tick", "unpark-all"])
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.window <= 10:
        parser.error("--window must be between 1 and 10 (refine_pending names at most ten)")

    project = load_project()
    labels = project.labels
    classes = load_task_classes()
    repo = project.repo
    issues = open_issues(repo)
    blocked = labels.blocked_on_human

    def is_open_task(issue: dict) -> bool:
        return "type:epic" not in label_names(issue) and blocked not in label_names(issue)

    parked = [i for i in issues if PARKED in label_names(i) and is_open_task(i)]
    queued = [
        i for i in issues
        if labels.refine in label_names(i) and is_open_task(i) and needs_refining(i, classes)
    ]

    if args.mode == "unpark-all":
        for issue in parked:
            relabel(repo, issue["number"], labels.refine, PARKED, args.dry_run)
        return 0

    if args.mode == "tick":
        free = args.window - len(queued)
        if free <= 0 or not parked:
            return 0
        parents = parent_labels(repo, issues)
        for issue in sorted(parked, key=lambda i: rank(i, parents, labels.auto_ready))[:free]:
            relabel(repo, issue["number"], labels.refine, PARKED, args.dry_run)
        return 0

    # park
    if not args.dry_run:
        try:
            gh(f"repos/{repo}/labels", method="POST",
               fields=[f"name={PARKED}", "color=cccccc",
                       "description=Held out of status:refine by scripts/refine_window.py (refiner queue order)"])
        except RuntimeError:
            pass  # already exists
    parents = parent_labels(repo, issues)
    ordered = sorted(queued, key=lambda i: rank(i, parents, labels.auto_ready))
    print("window: " + ", ".join(f"#{i['number']}" for i in ordered[: args.window]))
    for issue in ordered[args.window:]:
        relabel(repo, issue["number"], PARKED, labels.refine, args.dry_run)
    return 0


GH = load_project().executables.get("gh") or "gh"

if __name__ == "__main__":
    sys.exit(main())
