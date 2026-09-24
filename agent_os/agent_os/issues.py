#!/usr/bin/env python3
"""GitHub Issues as the tracker, over `gh` (shell out, JSON in and out). Nothing here names a
project: the repository and the module vocabulary come from `config/agents.yaml`'s `project:`
section (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).

No third-party GitHub library and no new dependency: every call is `gh api` / `gh issue` /
`gh label` under `subprocess`.

    python -m agent_os.issues list [--all] [--type T] [--label L] [--state open|closed]
    python -m agent_os.issues show <N>
    python -m agent_os.issues create --type epic|feature|task|bug --title T [--parent N]
        [--body-file F | --template task|bug] [--label L ...]
    python -m agent_os.issues create --template task        # prints the scaffold, creates nothing
    python -m agent_os.issues update <N> [--state open|closed] [--comment "..."]
        [--add-label L] [--remove-label L] [--title T] [--body-file F]
    python -m agent_os.issues validate <N>
    python -m agent_os.issues move <N> refine|ready|doing|blocked-on-human|ai-completed|
        review|done
    python -m agent_os.issues brief <N> [--supplement F]
    python -m agent_os.issues load <backlog.yaml> [--dry-run]

Repository: the `AGENT_OS_GH_REPO` env variable (`owner/name`), else `project.repo` in
`config/agents.yaml`, else `gh repo view` on the cwd. Printed at the start of every command.

## The YAML->issue mapping (`load`)

The backlog YAML contract is unchanged (`scratchpad/ado_backlog_format.md`): `area`, `items` with
`key`/`type`/`title`/`state`/`description`/`tags`/`priority`/`target_date`/`start_date`/`children`
or `parent`. `flatten` and `validate` below hold that contract.

| YAML field          | GitHub                                                              |
|----------------------|---------------------------------------------------------------------|
| `type: Epic`         | label `type:epic`                                                    |
| `type: Issue`        | label `type:bug` if `tags` contains `bug`, else `type:feature`       |
| `type: Task`         | label `type:task`                                                    |
| `state: Done`        | issue closed                                                         |
| `state: Doing`       | issue open + label `status:doing`                                    |
| `state: To Do`       | issue open                                                           |
| `priority: n`        | label `p<n>`                                                         |
| `tags: [...]`        | labels verbatim (never a `key-*` tag)                                |
| `children` / `parent`| a GitHub sub-issue link (REST sub-issues API, see below)             |
| `target_date`        | a line in the body: `Target date: <date>`                            |

Idempotent by key: the key is stored in the body as the LAST line, `<!-- key: <slug> -->`. The
key -> issue number map is cached in `.cache/gh_keys.json` and, if lost, an individual key is
found again with `gh issue list --search '"<!-- key: KEY -->"'`.

## Sub-issues — verified 2026-09-13 against Context7 MCP (library `/websites/github_en_rest`,
source `https://docs.github.com/en/rest/issues/sub-issues`)

- `GET  /repos/{owner}/{repo}/issues/{issue_number}/sub_issues` lists a parent's children (each a
  full issue object; its `id`, not its `number`, is what the other two endpoints take).
- `POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues` with body `{"sub_issue_id": <id>}`
  (optional `replace_parent: true`) attaches the CHILD's `id` under the PARENT's `number`. Response
  201, same schema as "get parent issue".
- `DELETE /repos/{owner}/{repo}/issues/{issue_number}/sub_issue` — singular, unlike the other two —
  with body `{"sub_issue_id": <id>}` unlinks it. Response 200.

Both mutating calls risk secondary rate limiting per GitHub's own docs, hence the sleep below.

## Labels

Created when missing, even if unused: `type:epic`, `type:feature`, `type:task`, `type:bug`,
`status:doing`, `p1`..`p4`, and `module:<name>` for every name in `project.modules`. Any other
tag from the YAML becomes a label too, created on first use.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import yaml

from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.lib import (
    STATES as WORK_STATES,
)
from agent_os.lib import (
    HumanMessageError,
    ProjectConfig,
    blocking_issue_numbers,
    label_names,
    load_project,
    load_task_classes,
    render_human_message,
    validate_issue_body,
)

# The HOST project's root, resolved rather than assumed: `$AGENT_OS_HOST_ROOT`, else the git
# checkout the call is made from. Everything a project owns hangs off it -- `config/agents.yaml`,
# `.cache/`, `.secrets/`, `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries -- and none of
# it is derived from this package's own location, which is what made the mechanism able to run
# exactly one project (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-
# never-modified.md).
HOST_ROOT = host_root()
TEMPLATE_DIR = HOST_ROOT / ".github" / "ISSUE_TEMPLATE"

KEYS_FILE = HOST_ROOT / ".cache" / "gh_keys.json"
# One marker file per issue already paged for review: what makes "once per issue" mechanical
# rather than something `move` has to remember, the same shape as the guard's own daily cap
# marker. Under `.cache/`, so it is gitignored and a fresh clone simply pages again.
REVIEW_PAGES_DIR = HOST_ROOT / ".cache" / "paged-review"
KEY_LINE_RE = re.compile(r"<!--\s*key:\s*([a-z0-9][a-z0-9.-]*)\s*-->")

TYPES = ("Epic", "Issue", "Task")  # the backlog YAML vocabulary, unchanged
STATES = ("To Do", "Doing", "Done")


def type_labels(project: ProjectConfig | None = None) -> dict[str, str]:
    """`epic`/`feature`/`task`/`bug` -> their `type:<name>` label, from `project.labels.types`
    (`config/agents.yaml`) rather than a literal dict (`agent_os/docs/AGENT_OS.md` §7 row (c)'s sibling,
    #510). The four CONCEPTS are the backlog YAML's own fixed vocabulary -- `type_label` below
    decides which one an entry is -- and do not change with the project; what a project configures
    is which of them get a label and under what name, plus the `type:` prefix stays fixed."""
    project = project or load_project()
    return {name: f"type:{name}" for name in project.labels.types}


def fixed_labels(project: ProjectConfig | None = None) -> list[str]:
    """The labels this tracker creates whether or not anything uses them yet. The `module:<name>`
    half is `project.modules` in `config/agents.yaml`, the in-progress state is
    `project.labels.doing`, and the `type:*`/`p<n>`/`module:` vocabulary itself is
    `project.labels.{types,priorities,module_prefix}` -- never a list or a literal in this file: a
    second project writes its own names there and changes no code (`agent_os/docs/AGENT_OS.md` §7 row (c)).
    """
    project = project or load_project()
    return (
        list(type_labels(project).values())
        + [project.labels.doing]
        + list(project.labels.priorities)
        + [f"{project.labels.module_prefix}{name}" for name in project.modules]
    )


# --------------------------------------------------------------------------------------------
# The backlog YAML: flatten and validate
# --------------------------------------------------------------------------------------------


def flatten(items: list[dict], parent_key: str | None = None) -> list[dict]:
    flat: list[dict] = []
    for item in items:
        entry = {k: v for k, v in item.items() if k != "children"}
        if parent_key and "parent" not in entry:
            entry["parent"] = parent_key
        flat.append(entry)
        flat.extend(flatten(item.get("children", []), entry["key"]))
    return flat


def validate(entries: list[dict], known: dict[str, int]) -> None:
    keys = [entry.get("key") for entry in entries]
    problems = []
    for entry in entries:
        key = entry.get("key")
        if not key or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", str(key)):
            problems.append(f"bad or missing key: {entry!r}"[:160])
        if entry.get("type") not in TYPES:
            problems.append(f"{key}: type must be one of {TYPES}")
        if not entry.get("title"):
            problems.append(f"{key}: title missing")
        if entry.get("state", "To Do") not in STATES:
            problems.append(f"{key}: state must be one of {STATES}")
        if entry.get("parent") and entry["parent"] not in keys and entry["parent"] not in known:
            problems.append(
                f"{key}: parent {entry['parent']} is neither in this file nor already loaded"
            )
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        problems.append(f"duplicate keys: {sorted(duplicates)}")
    if problems:
        sys.exit("backlog file rejected:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------------------------
# gh subprocess plumbing
# --------------------------------------------------------------------------------------------


def _gh(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, input=input_text, check=False
    )


def _is_rate_limited(message: str) -> bool:
    lowered = message.lower()
    return "403" in message or "429" in message or "rate limit" in lowered


def _retry_after(message: str) -> float | None:
    match = re.search(r"retry.after[:\s]+(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    return float(match.group(1)) if match else None


def gh_json(*args: str, input_text: str | None = None):
    """Runs `gh <args>`, parses stdout as JSON (None if empty), retries up to 5 times on a rate
    limit (honouring `Retry-After` if present, exponential backoff otherwise), and exits on any
    other failure."""
    attempts = 6
    for attempt in range(attempts):
        result = _gh(*args, input_text=input_text)
        if result.returncode == 0:
            text = result.stdout.strip()
            return json.loads(text) if text else None
        if attempt < attempts - 1 and _is_rate_limited(result.stderr):
            time.sleep(_retry_after(result.stderr) or (2**attempt))
            continue
        sys.exit(f"gh {' '.join(args)} failed:\n{result.stderr}")


def gh_json_dict(*args: str, input_text: str | None = None) -> dict:
    """Like `gh_json`, narrowed to the common case of a single JSON object: every endpoint used
    this way (create/update an issue, view one) always returns an object on success, so an empty
    or non-object response means something already went wrong upstream."""
    result = gh_json(*args, input_text=input_text)
    if not isinstance(result, dict):
        sys.exit(f"gh {' '.join(args)} returned no JSON object")
    return result


def repo_name() -> str:
    """`owner/name`, resolved in three steps: the `AGENT_OS_GH_REPO` environment variable, which
    points one run at another repository without editing anything; then `project.repo` in
    `config/agents.yaml`, so a command run from outside the clone still reaches the right tracker;
    then `gh repo view` on the cwd, which is all that is left when neither is set. The env variable
    carries no project's name, and the configured value is finally read rather than decorative
    (`agent_os/docs/AGENT_OS.md` §7 row (d))."""
    env = os.environ.get("AGENT_OS_GH_REPO")
    if env:
        return env
    configured = load_project().repo
    if configured:
        return configured
    result = _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if result.returncode != 0:
        sys.exit(
            "no repo: set AGENT_OS_GH_REPO, fill project.repo in config/agents.yaml, "
            f"or run inside a GitHub repo\n{result.stderr}"
        )
    return result.stdout.strip()


# --------------------------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------------------------


def existing_labels(repo: str) -> set[str]:
    rows = gh_json("label", "list", "--repo", repo, "--limit", "200", "--json", "name") or []
    return {row["name"] for row in rows}


def ensure_labels(repo: str, names: list[str], cache: set[str]) -> None:
    for name in dict.fromkeys(names):
        if name in cache:
            continue
        _gh("label", "create", name, "--repo", repo, "--force")
        cache.add(name)
        time.sleep(1)


def ensure_fixed_labels(repo: str) -> set[str]:
    cache = existing_labels(repo)
    ensure_labels(repo, fixed_labels(), cache)
    return cache


def sub_issue_numbers(sub_issues) -> list[int]:
    """`gh issue view --json subIssues` does not return a plain list: it returns the GraphQL
    connection verbatim, `{"nodes": [...], "totalCount": n}`, so iterating it yields the strings
    "nodes"/"totalCount" and indexing one by ["number"] raises (issue #343). Accept both shapes —
    a bare list is what the REST sub-issues endpoint gives — and ignore anything else."""
    if isinstance(sub_issues, dict):
        sub_issues = sub_issues.get("nodes") or []
    if not isinstance(sub_issues, list):
        return []
    return [c["number"] for c in sub_issues if isinstance(c, dict) and "number" in c]


def parse_key_line(body: str | None) -> str | None:
    if not body:
        return None
    matches = KEY_LINE_RE.findall(body)
    return matches[-1] if matches else None


def compose_body(entry: dict, key: str) -> str:
    body = str(entry.get("description", "")).rstrip()
    if entry.get("target_date"):
        body = f"{body}\n\nTarget date: {entry['target_date']}".strip()
    body = body.strip()
    return f"{body}\n\n<!-- key: {key} -->" if body else f"<!-- key: {key} -->"


# --------------------------------------------------------------------------------------------
# YAML entry -> GitHub fields
# --------------------------------------------------------------------------------------------


def type_label(entry: dict, project: ProjectConfig | None = None) -> str:
    labels = type_labels(project)
    kind = entry["type"]
    if kind == "Epic":
        return labels["epic"]
    if kind == "Task":
        return labels["task"]
    tags = [str(tag).lower() for tag in entry.get("tags", [])]
    return labels["bug"] if "bug" in tags else labels["feature"]


def desired_labels(entry: dict, project: ProjectConfig | None = None) -> list[str]:
    project = project or load_project()
    labels = [type_label(entry, project)]
    state = entry.get("state", "To Do")
    if state == "Doing":
        # The same configured vocabulary `move` writes, never a second spelling of it.
        labels.append(project.labels.doing)
    if entry.get("priority"):
        # 1-based, matching the YAML's own `priority: n`.
        labels.append(project.labels.priorities[int(entry["priority"]) - 1])
    for tag in entry.get("tags", []):
        tag = str(tag)
        if not tag.startswith("key-"):
            labels.append(tag)
    return list(dict.fromkeys(labels))


def desired_gh_state(entry: dict) -> str:
    return "closed" if entry.get("state", "To Do") == "Done" else "open"


# --------------------------------------------------------------------------------------------
# Issue CRUD
# --------------------------------------------------------------------------------------------


def find_by_key(repo: str, key: str) -> int | None:
    needle = f"<!-- key: {key} -->"
    rows = (
        gh_json(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--search",
            f'"{needle}"',
            "--json",
            "number,body",
            "--limit",
            "10",
        )
        or []
    )
    for row in rows:
        if parse_key_line(row.get("body", "")) == key:
            return row["number"]
    return None


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> dict:
    args = [
        "api",
        f"repos/{repo}/issues",
        "-X",
        "POST",
        "-f",
        f"title={title}",
        "-f",
        f"body={body}",
    ]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    result = gh_json_dict(*args)
    time.sleep(1)
    return result


def update_issue(repo: str, number: int, fields: dict) -> dict:
    path = f"repos/{repo}/issues/{number}"
    # `-f labels[]=x` repeated builds an array but cannot express an EMPTY one, and taking the
    # last label off an issue is exactly what `move <N> done` does to one whose only label was a
    # status label. A raw JSON body can say `[]`, so that is the path when the list is empty.
    if fields.get("labels") == []:
        result = gh_json_dict(
            "api", path, "-X", "PATCH", "--input", "-", input_text=json.dumps(fields)
        )
    else:
        args = ["api", path, "-X", "PATCH"]
        for name, value in fields.items():
            if name == "labels":
                for label in value:
                    args += ["-f", f"labels[]={label}"]
            else:
                args += ["-f", f"{name}={value}"]
        result = gh_json_dict(*args)
    time.sleep(1)
    return result


def get_sub_issue_ids(repo: str, parent_number: int) -> set[int]:
    rows = gh_json("api", f"repos/{repo}/issues/{parent_number}/sub_issues", "--paginate") or []
    return {row["id"] for row in rows}


def add_sub_issue(repo: str, parent_number: int, child_id: int) -> None:
    gh_json(
        "api",
        f"repos/{repo}/issues/{parent_number}/sub_issues",
        "-X",
        "POST",
        "-F",
        f"sub_issue_id={child_id}",
    )
    time.sleep(1)


def remove_sub_issue(repo: str, parent_number: int, child_id: int) -> None:
    gh_json(
        "api",
        f"repos/{repo}/issues/{parent_number}/sub_issue",
        "-X",
        "DELETE",
        "-F",
        f"sub_issue_id={child_id}",
    )
    time.sleep(1)


def link_parent(repo: str, parent_number: int, child_number: int, child_id: int) -> None:
    current = _gh(
        "issue",
        "view",
        str(child_number),
        "--repo",
        repo,
        "--json",
        "parent",
        "--jq",
        '.parent.number // ""',
    )
    current_parent = (
        int(current.stdout.strip())
        if current.returncode == 0 and current.stdout.strip().isdigit()
        else None
    )
    if current_parent == parent_number:
        return
    if current_parent:
        remove_sub_issue(repo, current_parent, child_id)
    add_sub_issue(repo, parent_number, child_id)


def sync_entry(
    repo: str, entry: dict, keys: dict[str, int], label_cache: set[str], dry_run: bool
) -> str | None:
    """Creates or updates one issue for `entry` (the backlog YAML entry shape), links its
    parent if any, and records its number in `keys` (mutated in place, saved to disk). Returns
    "created"/"updated", or None in dry-run (which never calls `gh`)."""
    key = entry["key"]
    title = str(entry["title"])[:255]
    ghtype = entry["type"]
    state = desired_gh_state(entry)
    number = keys.get(key)

    if dry_run:
        verb = "update" if number else "create"
        print(f"{verb:6} {ghtype:5} {state:6} {key:40} {title[:70]}")
        keys.setdefault(key, -1)
        return None

    labels = desired_labels(entry)
    ensure_labels(repo, labels, label_cache)
    body = compose_body(entry, key)

    if number is None:
        number = find_by_key(repo, key)

    if number is None:
        result = create_issue(repo, title, body, labels)
        number = result["number"]
        child_id = result["id"]
        if state == "closed":
            update_issue(repo, number, {"state": "closed"})
        verb = "created"
    else:
        result = update_issue(
            repo, number, {"title": title, "body": body, "labels": labels, "state": state}
        )
        child_id = result["id"]
        verb = "updated"

    print(f"{verb:7} #{number:<5} {ghtype:5} {state:6} {key:40} {title[:60]}")
    keys[key] = number
    save_keys(keys)

    if entry.get("parent"):
        parent_number = keys.get(entry["parent"])
        if parent_number and parent_number > 0:
            link_parent(repo, parent_number, number, child_id)

    return verb


def load_keys() -> dict[str, int]:
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text())
    return {}


def save_keys(keys: dict[str, int]) -> None:
    KEYS_FILE.parent.mkdir(exist_ok=True)
    KEYS_FILE.write_text(json.dumps(keys, indent=1, sort_keys=True))


# --------------------------------------------------------------------------------------------
# Paging the human (ntfy)
# --------------------------------------------------------------------------------------------
# agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md


def page_human(message: str) -> bool:
    """The ONE door to `agent_os/bin/notify.sh` in this module, and it never raises: the label and the
    board column are already written by the time anything pages, so a topic file that is missing
    or an ntfy.sh that is down must not turn a completed move into a failure. `message` always
    arrives rendered from `project.messages`; nothing here writes what a subscriber reads."""
    try:
        result = subprocess.run(
            [str(AGENT_OS_DIR / "bin" / "notify.sh"), message],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def page_review_ready(number: int, issue: dict) -> str:
    """The third ntfy trigger (2026-09-16): an approved PR waiting to be merged is the one thing
    only the human can finish, and until now nothing pushed to say so -- it was the touchpoint
    with the least warning in `agent_os/docs/AGENT_OS.md` §2.1. ONCE per issue, so a second `move N review`
    (a re-validation, a reopened PR) does not page again. Returns the line `move` prints.

    The marker is CLAIMED before the page goes out, with `open(..., "x")`: an exclusive create is
    one atomic step, where `exists()` then `write_text()` is two, and two concurrent moves of the
    same issue both passed the first one and both paged. Whoever creates it pages. A page that
    then fails releases the claim, so the next `move N review` may still try -- at the price that
    a rival who saw the marker in that window pages nobody, which is the right way round: a
    notification that arrives twice trains the habit of ignoring notifications, and that is what
    this whole channel exists not to do."""
    try:
        message = render_human_message(
            "review_ready",
            issue=number,
            title=issue.get("title") or "",
            url=issue.get("url") or "",
        )
    except HumanMessageError as error:
        # A page the project cannot write is a defect in `config/agents.yaml`, not in this move:
        # the label and the board column are already saved, and this says so out loud instead.
        return f"ntfy:     not sent -- {error}"
    marker = REVIEW_PAGES_DIR / str(number)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x") as claim:
            claim.write(f"{message}\n")
    except FileExistsError:
        return f"ntfy:     #{number} was already paged for review, not paging again"
    if not page_human(message):
        marker.unlink(missing_ok=True)
        return "ntfy:     agent_os/bin/notify.sh failed; the move stands, nobody was paged"
    return f"ntfy:     {message}"


# --------------------------------------------------------------------------------------------
# Templates, validation, mechanical state
# --------------------------------------------------------------------------------------------
# agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-state.md

FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def template_body(name: str) -> str:
    """The scaffold `.github/ISSUE_TEMPLATE/<name>.md` holds, with GitHub's YAML front matter
    (`name`, `about`, `title`, `labels`) stripped: the front matter configures GitHub's own "new
    issue" form and would be noise inside a body created through the API."""
    text = (TEMPLATE_DIR / f"{name}.md").read_text()
    return FRONT_MATTER_RE.sub("", text).strip() + "\n"


def open_issue_numbers(repo: str) -> set[int]:
    """Every open issue's number, one listing, so `Blocked by #N` resolves without a `gh issue
    view` per blocker."""
    rows = gh_json(
        "issue", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json", "number"
    )
    return {int(row["number"]) for row in rows or []}


def validate_issue(repo: str, number: int) -> list[str]:
    """Every mechanical reason issue #N is not a brief an agent could start from, one per line.
    The rules themselves live in `agent_lib.validate_issue_body`, which is also what the guard's
    dispatchable predicate asks — "Ready for AI" and "the validator passes" are one implementation
    and cannot drift apart."""
    data = gh_json_dict("issue", "view", str(number), "--repo", repo, "--json", "body,labels")
    labels = type_labels()
    grouping_types = [
        label for label in sorted(label_names(data)) if label in (labels["epic"], labels["feature"])
    ]
    if grouping_types:
        # An epic or a feature groups briefs and has no template of its own: reporting the task
        # sections it lacks would read as a defect when it is simply not a brief. Still a failure,
        # so `worker_task.sh start` keeps refusing to dispatch one.
        not_a_brief = f"#{number} is {grouping_types[0]}, not a brief"
        return [f"{not_a_brief}: only type:task and type:bug issues are validated"]
    body = data.get("body") or ""
    blockers = open_issue_numbers(repo) if blocking_issue_numbers(body) else set()
    return validate_issue_body(body, task_classes=load_task_classes(), open_issue_numbers=blockers)


# --------------------------------------------------------------------------------------------
# The board mirror
# --------------------------------------------------------------------------------------------
# The labels ARE the state; the project board is a mirror for the human to read, written here and
# never read back by the guard or the planner. The board number, the owner (from the repository
# slug) and the column names all come from `config/agents.yaml`'s `project:` section.

BOARD_STATUS_FIELD = "Status"


def board_owner(repo: str) -> str:
    return repo.split("/", 1)[0]


def board_status_field(owner: str, board: int) -> tuple[str, str, dict[str, str]]:
    """`(project id, Status field id, {option name: option id})`, queried once per run. The
    single-select field GitHub creates with every project board is named `Status`; a board whose
    column field is named something else falls back to its first single-select field."""
    project = gh_json_dict("project", "view", str(board), "--owner", owner, "--format", "json")
    fields = (
        gh_json_dict("project", "field-list", str(board), "--owner", owner, "--format", "json").get(
            "fields"
        )
        or []
    )
    single_selects = [field for field in fields if field.get("options") is not None]
    status = next(
        (field for field in single_selects if field.get("name") == BOARD_STATUS_FIELD),
        next(iter(single_selects), None),
    )
    if not status:
        sys.exit(f"project {owner}/{board} has no single-select field to mirror the state into")
    options = {option["name"]: option["id"] for option in status.get("options") or []}
    return project["id"], status["id"], options


def board_item_id(owner: str, board: int, repo: str, number: int) -> str | None:
    """The board item holding issue #N, or None when the issue was never added to the board —
    which is not an error: the labels carry the state, the board only shows it."""
    data = gh_json(
        "project",
        "item-list",
        str(board),
        "--owner",
        owner,
        "--format",
        "json",
        "--limit",
        "1000",
    )
    for item in (data or {}).get("items") or []:
        content = item.get("content") or {}
        if content.get("number") == number and content.get("repository") in (None, repo):
            return item["id"]
    return None


def mirror_board_column(repo: str, number: int, column: str, board: int) -> str:
    """Moves issue #N's board item to `column`. Returns the line to print: what it did, or why it
    could not, never an exception — a board that disagrees with the labels is a cosmetic defect,
    and failing the `move` here would leave the label set and the caller thinking it was not."""
    owner = board_owner(repo)
    item = board_item_id(owner, board, repo, number)
    if not item:
        return f"board:    #{number} is not on project {board}; column not mirrored"
    project_id, field_id, options = board_status_field(owner, board)
    if column not in options:
        return f"board:    no column named '{column}' on project {board}; column not mirrored"
    gh_json(
        "project",
        "item-edit",
        "--id",
        item,
        "--project-id",
        project_id,
        "--field-id",
        field_id,
        "--single-select-option-id",
        options[column],
    )
    return f"board:    {column}"


# --------------------------------------------------------------------------------------------
# The brief a worker starts from
# --------------------------------------------------------------------------------------------


def compose_brief(issue: dict, parent: dict | None, supplement: str | None) -> str:
    """Issue body, then parent body, then the optional supplement — the whole of what a worker
    reads after `AGENTS.md`
    (agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-
    state.md). The parent section is always emitted, saying so when there is no parent: a missing
    section would read as "the assembly failed" exactly like "there is nothing above this".
    """

    def block(heading: str, data: dict) -> str:
        url = data.get("url") or ""
        body = (data.get("body") or "").strip() or "_(this issue has an empty body)_"
        return f"# {heading} — {data.get('title', '')}\n{url}\n\n{body}\n"

    parts = [block(f"Issue #{issue['number']}", issue)]
    parts.append(
        block(f"Parent issue #{parent['number']}", parent)
        if parent
        else "# Parent issue\n\n_(this issue has no parent)_\n"
    )
    if supplement:
        parts.append(f"## Supplement\n\n{supplement.strip()}\n")
    return "\n".join(parts)


def fetch_brief_sources(repo: str, number: int) -> tuple[dict, dict | None]:
    issue = gh_json_dict(
        "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url,parent"
    )
    parent_ref = issue.get("parent")
    if not parent_ref:
        return issue, None
    parent = gh_json_dict(
        "issue",
        "view",
        str(parent_ref["number"]),
        "--repo",
        repo,
        "--json",
        "number,title,body,url",
    )
    return issue, parent


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    state = args.state or ("all" if args.all else "open")
    gh_args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        "1000",
        "--json",
        "number,title,state,labels,body",
    ]
    if args.label:
        gh_args += ["--label", args.label]
    rows = gh_json(*gh_args) or []
    printed = 0
    for row in rows:
        labels = [label["name"] for label in row.get("labels", [])]
        rtype = next((label.split(":", 1)[1] for label in labels if label.startswith("type:")), "?")
        if args.type and rtype != args.type:
            continue
        key = parse_key_line(row.get("body", "")) or ""
        print(f"#{row['number']:<5} {rtype:8} {row['state']:6} {key:30} {row['title'][:70]}")
        printed += 1
    if not printed:
        print("no issues match")


def cmd_show(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    data = gh_json_dict(
        "issue",
        "view",
        str(args.number),
        "--repo",
        repo,
        "--json",
        "number,title,state,labels,body,parent,subIssues,url",
    )
    labels = ", ".join(label["name"] for label in data.get("labels", []))
    parent = data.get("parent")
    children = sub_issue_numbers(data.get("subIssues"))
    print(f"number   #{data['number']}")
    print(f"title    {data['title']}")
    print(f"state    {data['state']}")
    print(f"labels   {labels}")
    print(f"key      {parse_key_line(data.get('body', '')) or ''}")
    print(f"parent   {'#' + str(parent['number']) if parent else ''}")
    print(f"children {', '.join('#' + str(number) for number in children)}")
    print(f"url      {data.get('url', '')}")
    print("body")
    print(data.get("body", ""))


def cmd_create(args: argparse.Namespace) -> None:
    if args.template and args.body_file:
        sys.exit("--template and --body-file are two bodies; pass one")
    issue_type = args.type or args.template
    if not issue_type:
        sys.exit("pass --type, or --template (which implies it)")
    # A `--template` with no title creates nothing: it prints the scaffold, to be redirected to a
    # file, filled in, and passed back as `--body-file`.
    if args.template and not args.title:
        print(template_body(args.template), end="")
        return
    if not args.title:
        sys.exit("--title is required")
    repo = repo_name()
    print(f"repo: {repo}")
    label_cache = ensure_fixed_labels(repo)
    labels = [type_labels()[issue_type]] + list(args.label or [])
    ensure_labels(repo, labels, label_cache)
    if args.template:
        body = template_body(args.template)
    else:
        body = pathlib.Path(args.body_file).read_text() if args.body_file else ""
    result = create_issue(repo, args.title, body, labels)
    number = result["number"]
    print(f"created #{number}  {result.get('html_url', '')}")
    if args.parent:
        link_parent(repo, args.parent, number, result["id"])


def cmd_update(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    current = gh_json_dict(
        "issue", "view", str(args.number), "--repo", repo, "--json", "labels,title"
    )
    labels = [label["name"] for label in current.get("labels", [])]
    if args.add_label:
        label_cache = ensure_fixed_labels(repo)
        ensure_labels(repo, args.add_label, label_cache)
        for label in args.add_label:
            if label not in labels:
                labels.append(label)
    if args.remove_label:
        drop = set(args.remove_label)
        labels = [label for label in labels if label not in drop]

    fields: dict = {}
    if args.add_label or args.remove_label:
        fields["labels"] = labels
    if args.title:
        fields["title"] = args.title
    if args.state:
        fields["state"] = args.state
    if args.body_file:
        fields["body"] = pathlib.Path(args.body_file).read_text()
    if fields:
        update_issue(repo, args.number, fields)
    if args.comment:
        gh_json(
            "api",
            f"repos/{repo}/issues/{args.number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={args.comment}",
        )
    print(f"updated #{args.number}")


def cmd_validate(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    failures = validate_issue(repo, args.number)
    for failure in failures:
        print(failure)
    if failures:
        sys.exit(1)
    print("ok")


def cmd_move(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    project = load_project()
    vocabulary = project.labels
    target = vocabulary.label_for_state(args.state)
    current = gh_json_dict(
        "issue", "view", str(args.number), "--repo", repo, "--json", "labels,state,title,url"
    )
    # Exactly one state label at a time: every other one comes off, whatever it was, so an issue
    # can never read as two states at once. `status:agents-paused` is not a state and is never
    # touched here -- it is the human-only full stop on the tracking epic.
    held = [label["name"] for label in current.get("labels", [])]
    labels = [label for label in held if label not in set(vocabulary.state_labels)]
    if target:
        ensure_labels(repo, [target], existing_labels(repo))
        labels.append(target)
    fields: dict = {"labels": labels}
    if args.state == "done" and (current.get("state") or "").upper() == "OPEN":
        fields["state"] = "closed"
    update_issue(repo, args.number, fields)
    print(
        f"labels:   {', '.join(labels) or '(none)'}" + ("  (closed)" if "state" in fields else "")
    )
    column = project.board_columns.get(args.state)
    if column:
        print(mirror_board_column(repo, args.number, column, project.board_number))
    else:
        print(f"board:    {args.state} keeps the item's current column")
    # Last, and only after the label and the board are written: the page announces a state the
    # tracker already holds, and it can never be the reason a move fails.
    if args.state == "review":
        print(page_review_ready(args.number, current))


def cmd_brief(args: argparse.Namespace) -> None:
    repo = repo_name()
    issue, parent = fetch_brief_sources(repo, args.number)
    supplement = pathlib.Path(args.supplement).read_text() if args.supplement else None
    text = compose_brief(issue, parent, supplement)
    if args.output:
        pathlib.Path(args.output).write_text(text)
    else:
        print(text, end="")


def cmd_load(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    document = yaml.safe_load(pathlib.Path(args.file).read_text())
    entries = flatten(document.get("items", []))
    keys = load_keys()
    validate(entries, keys)
    label_cache = set() if args.dry_run else ensure_fixed_labels(repo)
    created = updated = 0
    for entry in entries:
        verb = sync_entry(repo, entry, keys, label_cache, args.dry_run)
        created += verb == "created"
        updated += verb == "updated"
    if not args.dry_run:
        print(f"{created} created, {updated} updated, {len(entries)} in file; keys in {KEYS_FILE}")


def main() -> None:
    # The `--type` vocabulary is `project.labels.types`, read once here rather than per parser --
    # both `list` and `create` offer the same choices, and a broken config should fail before
    # either subcommand runs, not silently fall back to an empty list of choices.
    type_choices = list(load_project().labels.types)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.add_argument("--type", choices=type_choices)
    p.add_argument("--label")
    p.add_argument("--state", choices=("open", "closed"))
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("create")
    p.add_argument("--type", choices=type_choices)
    p.add_argument("--title")
    p.add_argument("--parent", type=int)
    p.add_argument("--body-file")
    p.add_argument(
        "--template", choices=("task", "bug"), help="scaffold from .github/ISSUE_TEMPLATE"
    )
    p.add_argument("--label", action="append")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update")
    p.add_argument("number", type=int)
    p.add_argument("--state", choices=("open", "closed"))
    p.add_argument("--comment")
    p.add_argument("--add-label", action="append")
    p.add_argument("--remove-label", action="append")
    p.add_argument("--title")
    p.add_argument("--body-file", help="replace the body with this file's contents")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("validate")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("move")
    p.add_argument("number", type=int)
    p.add_argument("state", choices=WORK_STATES)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("brief")
    p.add_argument("number", type=int)
    p.add_argument("--supplement", help="an extra brief, appended under `## Supplement`")
    p.add_argument("--output", help="write here instead of stdout")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("load")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_load)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
