"""Cheap board lookups for `agent_os.issues` (host-side workaround for titanarq/agent-os#27).

`issues.py move` mirrors the new state into the Project v2 board through `mirror_board_column`,
which resolves two things with `gh project` subcommands whose GraphQL cost grows with the board:

- `board_item_id`: `gh project item-list <board> --limit 1000` -- ~108 points on project 3
  (91 items, measured 2026-09-24);
- `board_status_field`: `gh project view` + `gh project field-list` -- ~3 + ~103 points.

Together they made one `move` cost ~224 points of the owner's shared 5000/h GraphQL quota. The
replacements below ask the same questions from the other side, one small query each (~1 point):
the issue's own `projectItems`, and the board's `Status` field by name.

Every replacement falls back to the mechanism's original function on ANY error (a failed `gh`,
unexpected JSON, an owner that is neither user nor org, a board without a field named `Status`),
so the worst case is the old cost, never a failed move. A clean "the issue has no item on this
board" answer is a real answer (`None`), not an error, and does not fall back. Archived items are
left out, as `gh project item-list` leaves them out.

`install` checks the module still has every attribute it patches or calls (`PATCHED`, `_gh`);
when a subtree pull renamed one, it warns once and patches nothing, so `issues.py` runs as the
mechanism ships it (the old cost) instead of every call crashing.

`install(issues_module)` swaps both functions in the module's globals, which is where
`mirror_board_column` looks them up. It is applied by `issues_main.py`, the entry point the
`python` wrapper beside this file runs instead of `-m agent_os.issues`. Remove this directory
once agent-os#27 is fixed and the subtree is pulled.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from types import ModuleType

ITEM_QUERY = """query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){issue(number:$number){
    projectItems(first:20,includeArchived:false){nodes{id isArchived project{number owner{
      ... on Organization{login} ... on User{login}}}}}}}}"""

FIELD_QUERY = """query($owner:String!,$board:Int!){
  repositoryOwner(login:$owner){... on ProjectV2Owner{projectV2(number:$board){id
    field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}}}}}"""

# Set by `install`: the mechanism's module, whose `_gh` runs `gh` (and which the tests fake).
_issues: ModuleType | None = None
# The originals, kept for the fallback.
_original_item_id: Callable | None = None
_original_status_field: Callable | None = None


# What `install` swaps, and what the replacements call; all must exist in `agent_os.issues`.
PATCHED = ("board_item_id", "board_status_field")
REQUIRED = (*PATCHED, "_gh")


class CheapLookupFailed(Exception):  # noqa: N818 -- a signal to fall back, not an error to report
    """Anything that makes the cheap answer untrustworthy; always means "use the original"."""


def _warn(what: str, error: Exception) -> None:
    print(f"agent_os_patches: cheap {what} failed ({error}); using the original", file=sys.stderr)


def _graphql(query: str, **variables: object) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    assert _issues is not None
    result = _issues._gh(*args)
    if result.returncode != 0:
        stderr = result.stderr.strip()[:200]
        raise CheapLookupFailed(f"gh api graphql exited {result.returncode}: {stderr}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CheapLookupFailed(f"not JSON: {error}") from error
    if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
        raise CheapLookupFailed(f"graphql errors: {json.dumps(data)[:200]}")
    return data["data"]


def cheap_item_id(owner: str, board: int, repo: str, number: int) -> str | None:
    """The id of issue #N's item on `owner`'s board `board`, or None when it has none there.
    Raises CheapLookupFailed when the answer cannot be trusted."""
    repo_owner, _, name = repo.partition("/")
    data = _graphql(ITEM_QUERY, owner=repo_owner, name=name, number=number)
    issue = (data.get("repository") or {}).get("issue")
    if not isinstance(issue, dict):
        # A pull request number, a transferred issue, no access: let the original decide.
        raise CheapLookupFailed(f"{repo}#{number} is not an issue this token can see")
    nodes = (issue.get("projectItems") or {}).get("nodes")
    if not isinstance(nodes, list):
        raise CheapLookupFailed("no projectItems connection in the answer")
    if len(nodes) >= 20:
        # first:20 may have cut the list; an issue on 20+ boards is not worth a second query.
        raise CheapLookupFailed("issue is on 20 or more projects")
    for node in nodes:
        if (node or {}).get("isArchived"):
            continue
        project = (node or {}).get("project") or {}
        login = ((project.get("owner") or {}).get("login") or "").lower()
        if project.get("number") == board and login == owner.lower():
            return node["id"]
    return None


def cheap_status_field(owner: str, board: int) -> tuple[str, str, dict[str, str]]:
    """`(project id, Status field id, {option name: option id})`, as `board_status_field` returns.
    Raises CheapLookupFailed when the board has no single-select field named `Status` (the
    original's first-single-select fallback is then the original's job)."""
    data = _graphql(FIELD_QUERY, owner=owner, board=board)
    project = (data.get("repositoryOwner") or {}).get("projectV2")
    if not isinstance(project, dict) or not project.get("id"):
        raise CheapLookupFailed(f"no project {owner}/{board}")
    field = project.get("field")
    if not isinstance(field, dict) or not field.get("id") or field.get("options") is None:
        raise CheapLookupFailed(f"project {owner}/{board} has no single-select field named Status")
    options = {option["name"]: option["id"] for option in field["options"]}
    return project["id"], field["id"], options


def board_item_id(owner: str, board: int, repo: str, number: int) -> str | None:
    try:
        return cheap_item_id(owner, board, repo, number)
    except Exception as error:  # noqa: BLE001 -- any failure means the original's answer
        _warn("board item lookup", error)
        assert _original_item_id is not None
        return _original_item_id(owner, board, repo, number)


def board_status_field(owner: str, board: int) -> tuple[str, str, dict[str, str]]:
    try:
        return cheap_status_field(owner, board)
    except Exception as error:  # noqa: BLE001 -- any failure means the original's answer
        _warn("board Status field lookup", error)
        assert _original_status_field is not None
        return _original_status_field(owner, board)


def install(issues_module: ModuleType) -> bool:
    """Swap the two lookups in `agent_os.issues`; True when they are (now or already) swapped.
    Idempotent. When the module lacks anything in `REQUIRED`, warns once on stderr, changes
    nothing and returns False: the unpatched mechanism still works, only at the old cost."""
    global _issues, _original_item_id, _original_status_field
    if getattr(issues_module, "_agent_os_patches_board_lookup", False):
        return True
    missing = [name for name in REQUIRED if not callable(getattr(issues_module, name, None))]
    if missing:
        print(
            f"agent_os_patches: agent_os.issues has no {', '.join(missing)}; running it unpatched"
            " (board lookups at the old cost -- update scripts/agent_os_patches/)",
            file=sys.stderr,
        )
        return False
    _issues = issues_module
    _original_item_id = issues_module.board_item_id
    _original_status_field = issues_module.board_status_field
    issues_module.board_item_id = board_item_id
    issues_module.board_status_field = board_status_field
    issues_module._agent_os_patches_board_lookup = True
    return True
