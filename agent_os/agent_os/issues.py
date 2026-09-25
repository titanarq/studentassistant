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
    python -m agent_os.issues move <N> [<N> ...] refine|ready|doing|blocked-on-human|
        ai-completed|review|done      # several numbers: one invocation, the board resolved once
    python -m agent_os.issues brief <N> [--supplement F]
    python -m agent_os.issues load <backlog.yaml> [--dry-run]
    python -m agent_os.issues supersede <N> --by A [--by B ...] [--route D=A[,B] ...]

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
import datetime
import functools
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

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
    replace_blocker,
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
    """A rate limit, primary or secondary, by what GitHub says -- not by a bare 403, which is as
    often a permission refused, where retrying only delays the same answer."""
    return "429" in message or "rate limit" in message.lower()


def _is_secondary_rate_limit(message: str) -> bool:
    """GitHub's concurrency / points-per-minute limit: it has no bucket to read, and GitHub asks
    for at least a minute's wait when it sends no `Retry-After`."""
    return "secondary rate limit" in message.lower()


# GitHub's documented minimum wait after a secondary rate limit that carries no `Retry-After`.
SECONDARY_RATE_LIMIT_WAIT_SECONDS = 60


def _retry_after(message: str) -> float | None:
    match = re.search(r"retry.after[:\s]+(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    return float(match.group(1)) if match else None


def exhausted_quotas() -> list[str] | None:
    """One line per quota of this `gh` login that is at zero right now -- `graphql: 0 of 5000
    left, resets at ...` -- or None when the quotas could not be read. `gh api rate_limit` is
    free: it counts against none of them.

    Every API has its own bucket: `core` (REST), `graphql`, `search`... The top-level `.rate`
    that `gh api rate_limit` prints first is `core` alone, so it can read `remaining: 5000` while
    `graphql` is at zero, and every GraphQL-backed `gh` subcommand answers "API rate limit already
    exceeded" (#70). Naming the empty bucket is what tells that apart from a transient refusal."""
    result = _gh("api", "rate_limit", "--jq", ".resources")
    if result.returncode != 0:
        return None
    try:
        resources = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(resources, dict):
        return None
    lines = []
    for name, bucket in sorted(resources.items()):
        if not isinstance(bucket, dict) or bucket.get("remaining") != 0:
            continue
        reset = datetime.datetime.fromtimestamp(int(bucket.get("reset") or 0), datetime.UTC)
        lines.append(
            f"{name}: 0 of {bucket.get('limit')} left, resets at {reset:%Y-%m-%dT%H:%M:%SZ}"
        )
    return lines


def gh_text(*args: str, input_text: str | None = None) -> str:
    """Runs `gh <args>` and returns its stripped stdout, and exits on any failure it cannot wait
    out. A rate limit is retried up to 5 times (`Retry-After` if present; at least a minute for a
    secondary limit; exponential backoff otherwise) -- unless one of the login's quotas is at
    zero, which no retry within the hour can outlast: that exits at once, naming the empty quota
    and when it refills, instead of sleeping on it and surfacing gh's bare text (#70)."""
    attempts = 6
    for attempt in range(attempts):
        result = _gh(*args, input_text=input_text)
        if result.returncode == 0:
            return result.stdout.strip()
        if not _is_rate_limited(result.stderr):
            break
        secondary = _is_secondary_rate_limit(result.stderr)
        empty = None if secondary else exhausted_quotas()
        if empty:
            sys.exit(
                f"gh {' '.join(args)} failed: this gh login's GitHub quota is exhausted, and "
                "retrying before it resets cannot succeed:\n"
                + "\n".join(f"  {line}" for line in empty)
                + f"\n{result.stderr}"
            )
        if attempt == attempts - 1:
            break
        backoff = float(2**attempt)
        if secondary:
            backoff = max(backoff, SECONDARY_RATE_LIMIT_WAIT_SECONDS)
        time.sleep(_retry_after(result.stderr) or backoff)
    sys.exit(f"gh {' '.join(args)} failed:\n{result.stderr}")


def gh_json(*args: str, input_text: str | None = None):
    """`gh_text`, parsed as JSON (None if empty)."""
    text = gh_text(*args, input_text=input_text)
    return json.loads(text) if text else None


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


# Labels are read over REST, never with `gh label list`: that one is GraphQL, and the GraphQL
# quota is 5000 points an hour shared by every host and tool the human runs, where REST draws on
# the separate core quota (#27).


def existing_labels(repo: str) -> set[str]:
    """Every label the repository has, over REST, every page of it."""
    text = gh_text("api", f"repos/{repo}/labels?per_page=100", "--paginate", "--jq", ".[].name")
    return {line for line in text.splitlines() if line}


def label_exists(repo: str, name: str) -> bool:
    """Whether the repository has label `name`: one REST `GET`, a 404 meaning no. What `move`
    asks about the one label it writes, instead of listing every label to find it."""
    result = _gh("api", f"repos/{repo}/labels/{urllib.parse.quote(name, safe='')}")
    if result.returncode == 0:
        return True
    if "404" in result.stderr or "not found" in result.stderr.lower():
        return False
    sys.exit(f"gh api repos/{repo}/labels/{name} failed:\n{result.stderr}")


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

FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def template_body(name: str) -> str:
    """The scaffold `.github/ISSUE_TEMPLATE/<name>.md` holds, with GitHub's YAML front matter
    (`name`, `about`, `title`, `labels`) stripped: the front matter configures GitHub's own "new
    issue" form and would be noise inside a body created through the API."""
    text = (TEMPLATE_DIR / f"{name}.md").read_text()
    return FRONT_MATTER_RE.sub("", text).strip() + "\n"


def template_title_prefix(name: str) -> str:
    """The `title:` GitHub's "new issue" form pre-fills from `.github/ISSUE_TEMPLATE/<name>.md`
    (`'[task] '` in the shipped task template), or "" when the type has no template or its front
    matter declares no title. Read from the host's template, so a host that spells its prefix
    differently is obeyed without a config key."""
    path = TEMPLATE_DIR / f"{name}.md"
    if not path.is_file():
        return ""
    match = FRONT_MATTER_RE.match(path.read_text())
    front_matter = yaml.safe_load(match.group(1)) if match else None
    title = front_matter.get("title") if isinstance(front_matter, dict) else None
    return title if isinstance(title, str) else ""


def prefixed_title(title: str, issue_type: str) -> str:
    """`title` with its type's template prefix in front, exactly once (#15). An issue created
    through the API skips GitHub's form, which is what applies the prefix to a hand-written one,
    so without this every refiner-created task lacked it. A title that already starts with the
    prefix — compared without its trailing space and ignoring case — is left as it is, so passing
    an already-prefixed title never yields `[task] [task] `."""
    prefix = template_title_prefix(issue_type)
    marker = prefix.strip()
    if not marker or title.lower().startswith(marker.lower()):
        return title
    return prefix + title


def open_issue_numbers(repo: str) -> set[int]:
    """Every open issue's number, one listing, so `Blocked by #N` resolves without a read per
    blocker. Over REST, every page of it: `gh issue list` is GraphQL (#70). REST lists open pull
    requests as issues too; they are left out, as `gh issue list` left them out."""
    text = gh_text(
        "api",
        f"repos/{repo}/issues?state=open&per_page=100",
        "--paginate",
        "--jq",
        ".[] | select(.pull_request == null) | .number",
    )
    return {int(line) for line in text.splitlines() if line.strip()}


def validate_issue(repo: str, number: int) -> list[str]:
    """Every mechanical reason issue #N is not a brief an agent could start from, one per line.
    The rules themselves live in `agent_lib.validate_issue_body`, which is also what the guard's
    dispatchable predicate asks — "Ready for AI" and "the validator passes" are one implementation
    and cannot drift apart.

    Read over REST, like `move` (#27): `worker_task.sh start` validates before every dispatch,
    and `gh issue view --json` is GraphQL -- a login whose GraphQL quota was empty could not
    dispatch at all while its REST quota was whole (#70)."""
    data = gh_json_dict("api", f"repos/{repo}/issues/{number}")
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


BOARD_FIELDS_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        fields(first: 50) {
          nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
        }
      }
    }
  }
}
"""


@functools.cache
def board_status_field(owner: str, board: int) -> tuple[str, str, dict[str, str]]:
    """`(project id, Status field id, {option name: option id})`, asked once per process and
    shared by every issue a bulk `move` touches. The single-select field GitHub creates with every
    project board is named `Status`; a board whose column field is named something else falls
    back to its first single-select field.

    One bounded GraphQL query (~1 point), not `gh project view` + `gh project field-list`: on a
    91-item board the field listing alone cost ~103 points of the user's 5000-an-hour GraphQL
    quota, which every host shares (#27)."""
    data = gh_json(
        "api",
        "graphql",
        "-f",
        f"query={BOARD_FIELDS_QUERY}",
        "-f",
        f"owner={owner}",
        "-F",
        f"number={board}",
    )
    project = (((data or {}).get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
    if not project:
        sys.exit(f"no project {owner}/{board} visible to this gh login to mirror the state into")
    fields = (project.get("fields") or {}).get("nodes") or []
    single_selects = [field for field in fields if field and field.get("options") is not None]
    status = next(
        (field for field in single_selects if field.get("name") == BOARD_STATUS_FIELD),
        next(iter(single_selects), None),
    )
    if not status:
        sys.exit(f"project {owner}/{board} has no single-select field to mirror the state into")
    options = {option["name"]: option["id"] for option in status.get("options") or []}
    return project["id"], status["id"], options


ISSUE_PROJECT_ITEMS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 50) {
        nodes { id project { number owner { ... on Organization { login } ... on User { login } } } }
      }
    }
  }
}
"""


def board_item_id(owner: str, board: int, repo: str, number: int) -> str | None:
    """The board item holding issue #N, or None when the issue was never added to the board —
    which is not an error: the labels carry the state, the board only shows it.

    Asked from the issue's side, not by listing the board (#14): `gh project item-list` on an org
    Project v2 came back with zero items while every issue's `projectItems` named its item there,
    so every `move` skipped the mirror in silence. One issue's items are also a bounded answer,
    where the listing stopped at its `--limit`."""
    repo_owner, name = repo.split("/", 1)
    data = gh_json(
        "api",
        "graphql",
        "-f",
        f"query={ISSUE_PROJECT_ITEMS_QUERY}",
        "-f",
        f"owner={repo_owner}",
        "-f",
        f"name={name}",
        "-F",
        f"number={number}",
    )
    issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue") or {}
    for item in (issue.get("projectItems") or {}).get("nodes") or []:
        project = item.get("project") or {}
        if project.get("number") == board and (project.get("owner") or {}).get("login") == owner:
            return item["id"]
    return None


def mirror_board_column(
    repo: str, number: int, column: str, board: int, *, item: str | None = None
) -> str:
    """Moves issue #N's board item to `column`. Returns the line to print: what it did, or why it
    could not, never an exception — a board that disagrees with the labels is a cosmetic defect,
    and failing the `move` here would leave the label set and the caller thinking it was not.

    `item` is the board item when the caller already holds it (`create`, from the `item-add` it
    just made); otherwise it is looked up from the issue's side."""
    owner = board_owner(repo)
    item = item or board_item_id(owner, board, repo, number)
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


def _exit_reason(exc: SystemExit) -> str:
    """The first line of what `gh_json` (or `board_status_field`) exited with: enough to say why
    in one `board:` line without dumping `gh`'s whole stderr into it."""
    text = str(exc.code) if exc.code is not None else ""
    return text.strip().splitlines()[0] if text.strip() else "gh failed"


def initial_board_column(labels: list[str], project: ProjectConfig) -> str | None:
    """The column the state label a new issue is created with maps to, or None when it carries
    no state label or that state keeps whatever column the item has (`blocked-on-human`)."""
    for state in WORK_STATES:
        label = project.labels.label_for_state(state)
        if label and label in labels:
            return project.board_columns.get(state)
    return None


def add_to_board(repo: str, number: int, url: str, labels: list[str]) -> list[str]:
    """Puts a just-created issue on `project.board_number` and, when it starts with a `status:*`
    label, sets the column that state mirrors (#23). Without this a later `move` found no item to
    mirror onto unless the host's board happened to auto-add issues. Returns the lines to print —
    never an exception: the issue exists and carries its labels whatever the board answers, and
    failing the `create` here would make the caller create it a second time."""
    project = load_project()
    board = project.board_number
    if not board:
        return []
    owner = board_owner(repo)
    try:
        added = gh_json(
            "project", "item-add", str(board), "--owner", owner, "--url", url, "--format", "json"
        )
    except SystemExit as exc:
        return [f"board:    #{number} not added to project {board}: {_exit_reason(exc)}"]
    lines = [f"board:    added #{number} to project {board}"]
    column = initial_board_column(labels, project)
    if column:
        item = (added or {}).get("id") if isinstance(added, dict) else None
        try:
            lines.append(mirror_board_column(repo, number, column, board, item=item))
        except SystemExit as exc:
            lines.append(f"board:    column not mirrored: {_exit_reason(exc)}")
    return lines


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
    result = create_issue(repo, prefixed_title(args.title, issue_type), body, labels)
    number = result["number"]
    print(f"created #{number}  {result.get('html_url', '')}")
    if args.parent:
        link_parent(repo, args.parent, number, result["id"])
    for line in add_to_board(repo, number, result.get("html_url", ""), labels):
        print(line)


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


def issue_for_move(repo: str, number: int) -> dict:
    """What `move` needs of issue #N -- its labels, state, title and web URL -- over REST: `gh
    issue view` is GraphQL, and that quota is the one a bulk move exhausted (#27)."""
    data = gh_json_dict("api", f"repos/{repo}/issues/{number}")
    return {
        "labels": data.get("labels") or [],
        "state": data.get("state") or "",
        "title": data.get("title") or "",
        "url": data.get("html_url") or "",
    }


def move_issue(repo: str, number: int, state: str, project: ProjectConfig) -> None:
    """Moves one issue to `state`: its label set, its open/closed state, its board column, and the
    review page. The target label is already known to exist -- `cmd_move` makes sure of it once
    for however many issues it moves."""
    vocabulary = project.labels
    target = vocabulary.label_for_state(state)
    current = issue_for_move(repo, number)
    # Exactly one state label at a time: every other one comes off, whatever it was, so an issue
    # can never read as two states at once. `status:agents-paused` is not a state and is never
    # touched here -- it is the human-only full stop on the tracking epic.
    held = [label["name"] for label in current["labels"]]
    labels = [label for label in held if label not in set(vocabulary.state_labels)]
    if target:
        labels.append(target)
    fields: dict = {"labels": labels}
    if state == "done" and current["state"].upper() == "OPEN":
        fields["state"] = "closed"
    update_issue(repo, number, fields)
    print(
        f"labels:   {', '.join(labels) or '(none)'}" + ("  (closed)" if "state" in fields else "")
    )
    column = project.board_columns.get(state)
    if column:
        print(mirror_board_column(repo, number, column, project.board_number))
    else:
        print(f"board:    {state} keeps the item's current column")
    # Last, and only after the label and the board are written: the page announces a state the
    # tracker already holds, and it can never be the reason a move fails.
    if state == "review":
        print(page_review_ready(number, current))


def cmd_move(args: argparse.Namespace) -> None:
    """`move N [N ...] STATE`. With several numbers the repository, the config, the target label
    and the board's Status field are resolved once for all of them (#27); each issue then costs
    two GraphQL requests (its board item, the column edit) and its REST reads and writes. One
    issue that fails does not stop the rest: its reason is printed under its number and the
    command exits non-zero naming every issue that did not move."""
    repo = repo_name()
    print(f"repo: {repo}")
    project = load_project()
    target = project.labels.label_for_state(args.state)
    if target and not label_exists(repo, target):
        ensure_labels(repo, [target], set())
    if len(args.numbers) == 1:
        move_issue(repo, args.numbers[0], args.state, project)
        return
    failed = []
    for number in args.numbers:
        print(f"#{number}")
        try:
            move_issue(repo, number, args.state, project)
        except SystemExit as exc:
            print(f"failed:   {exc.code}")
            failed.append(number)
    if failed:
        sys.exit(
            f"move {args.state}: {len(failed)} of {len(args.numbers)} failed: "
            + ", ".join(f"#{number}" for number in failed)
        )


def cmd_brief(args: argparse.Namespace) -> None:
    repo = repo_name()
    issue, parent = fetch_brief_sources(repo, args.number)
    supplement = pathlib.Path(args.supplement).read_text() if args.supplement else None
    text = compose_brief(issue, parent, supplement)
    if args.output:
        pathlib.Path(args.output).write_text(text)
    else:
        print(text, end="")


def parse_routes(routes: list[str] | None, children: list[int]) -> dict[int, list[int]]:
    """`--route D=A,B` pairs as {dependent: [children]}: which of the split's children a given
    dependent really waits on, when the refiner can tell. Every child named must be one of `--by`,
    so a typo can never point a dependent at an unrelated issue."""
    parsed: dict[int, list[int]] = {}
    for route in routes or []:
        dependent, _, targets = route.partition("=")
        try:
            numbers = [int(n.strip().lstrip("#")) for n in targets.split(",") if n.strip()]
            parsed[int(dependent.strip().lstrip("#"))] = numbers
        except ValueError:
            sys.exit(f"--route {route!r}: expected DEPENDENT=CHILD[,CHILD...]")
        if not numbers:
            sys.exit(f"--route {route!r} names no child")
        strangers = [n for n in numbers if n not in children]
        if strangers:
            listed = ", ".join(f"#{n}" for n in strangers)
            sys.exit(f"--route {route!r}: {listed} is not one of the --by children")
    return parsed


def supersede(
    repo: str, original: int, children: list[int], routes: dict[int, list[int]]
) -> list[str]:
    """The split's bookkeeping, done in one deterministic pass rather than left to a prompt (#39):
    every OPEN issue whose `## Dependencies` says `Blocked by #<original>` has that line rewritten
    to the children it is routed to (all of them when no route says otherwise) and gets a comment
    saying so; then the original is closed as `not planned` with a "superseded by" comment. Left
    open, the original blocked its dependents forever; closed by hand, it unblocked them before
    the children were done. Idempotent: a second run finds no dependent line and a closed original.
    Returns one line per thing it did."""
    if not children:
        sys.exit("supersede needs at least one --by child")
    if original in children:
        sys.exit(f"#{original} cannot supersede itself")
    current = gh_json_dict("issue", "view", str(original), "--repo", repo, "--json", "labels,state")
    labels = type_labels()
    grouping = sorted(label_names(current) & {labels["epic"], labels["feature"]})
    if grouping:
        # A feature's children are its parts, not its replacement: it stays open to group them.
        sys.exit(f"#{original} is {grouping[0]}: only a split task or bug is superseded")
    listing = ("issue", "list", "--repo", repo, "--state", "open", "--limit", "1000")
    rows = gh_json(*listing, "--json", "number,body") or []
    open_numbers = {int(row["number"]) for row in rows}
    missing = [n for n in children if n not in open_numbers]
    if missing:
        listed = ", ".join(f"#{n}" for n in missing)
        sys.exit(f"{listed} is not an open issue: a superseding child must exist and be open")
    unrouted = [d for d in routes if d not in open_numbers]
    if unrouted:
        sys.exit(f"--route names #{unrouted[0]}, which is not an open issue")
    children_text = ", ".join(f"#{n}" for n in children)
    done: list[str] = []
    for row in rows:
        dependent = int(row["number"])
        if dependent == original or dependent in children:
            continue
        targets = routes.get(dependent, children)
        body = replace_blocker(row.get("body") or "", original, targets)
        if body is None:
            continue
        update_issue(repo, dependent, {"body": body})
        targets_text = ", ".join(f"#{n}" for n in targets)
        comment = (
            f"`Blocked by #{original}` is now `Blocked by` {targets_text}: #{original} was split "
            f"into {children_text} and closed as superseded."
        )
        gh_json(
            "api",
            f"repos/{repo}/issues/{dependent}/comments",
            "-X",
            "POST",
            "-f",
            f"body={comment}",
        )
        done.append(f"dependent #{dependent}: Blocked by #{original} -> {targets_text}")
    if (current.get("state") or "").upper() == "OPEN":
        comment = f"Superseded by {children_text}."
        gh_json(
            "api", f"repos/{repo}/issues/{original}/comments", "-X", "POST", "-f", f"body={comment}"
        )
        update_issue(repo, original, {"state": "closed", "state_reason": "not_planned"})
        done.append(f"closed #{original} as not planned, superseded by {children_text}")
    else:
        done.append(f"#{original} was already closed")
    return done


def cmd_supersede(args: argparse.Namespace) -> None:
    repo = repo_name()
    print(f"repo: {repo}")
    children = list(dict.fromkeys(args.by or []))
    for line in supersede(repo, args.number, children, parse_routes(args.route, children)):
        print(line)


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
    p.add_argument("numbers", type=int, nargs="+", metavar="number")
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

    p = sub.add_parser(
        "supersede", help="after a split: repoint the original's dependents and close it"
    )
    p.add_argument("number", type=int)
    p.add_argument("--by", type=int, action="append", required=True, help="a child of the split")
    p.add_argument(
        "--route",
        action="append",
        help="DEPENDENT=CHILD[,CHILD]: the children one dependent waits on (default: all)",
    )
    p.set_defaults(func=cmd_supersede)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
