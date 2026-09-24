#!/usr/bin/env python3
"""Mirror every issue's `status:*` label into the Project v2 board's Status column.

Host-side workaround for titanarq/agent-os#14: `issues.py move` resolves the board item through
`gh project item-list`, which returns 0 items for this org project, so the column is never
mirrored. This script resolves items through GraphQL `issue.projectItems` instead, adds any issue
missing from the board, and sets its Status to the column `project.board_columns` in
`config/agents.yaml` maps its label to (no status label -> the refine column; closed -> done; a
label mapped to null, e.g. blocked-on-human, leaves the column as it is).

Idempotent and quiet: prints one line per change, nothing when the board already agrees.
Run by the `studentassistant-board-sync` user timer; safe to run by hand (`--dry-run` to preview).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GH = shutil.which("gh") or "/home/linuxbrew/.linuxbrew/bin/gh"


def gql(query: str, **variables) -> dict:
    args = [GH, "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"board_sync: gh api graphql failed: {out.stderr.strip()[:500]}")
    data = json.loads(out.stdout)
    if data.get("errors"):
        sys.exit(f"board_sync: graphql errors: {json.dumps(data['errors'])[:500]}")
    return data["data"]


def load_config() -> dict:
    with open(os.path.join(ROOT, "config", "agents.yaml")) as fh:
        return yaml.safe_load(fh)["project"]


def board(owner: str, number: int) -> tuple[str, str, dict[str, str]]:
    data = gql(
        """query($owner:String!,$n:Int!){organization(login:$owner){projectV2(number:$n){id
        field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}}}}""",
        owner=owner,
        n=number,
    )
    project = data["organization"]["projectV2"]
    field = project["field"]
    return project["id"], field["id"], {o["name"]: o["id"] for o in field["options"]}


def issues(owner: str, name: str) -> list[dict]:
    result, cursor = [], None
    while True:
        after = f',after:"{cursor}"' if cursor else ""
        data = gql(
            f"""query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{
            issues(first:100{after}){{pageInfo{{hasNextPage endCursor}} nodes{{id number state
            labels(first:30){{nodes{{name}}}}
            projectItems(first:10){{nodes{{id project{{id}}
            fieldValueByName(name:"Status"){{... on ProjectV2ItemFieldSingleSelectValue{{name}}}}}}}}
            }}}}}}}}""",
            owner=owner,
            name=name,
        )
        page = data["repository"]["issues"]
        result += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return result
        cursor = page["pageInfo"]["endCursor"]


def wanted_column(issue: dict, columns: dict, labels_cfg: dict) -> str | None | bool:
    """Column name, None to leave it alone, or False when nothing maps."""
    if issue["state"] == "CLOSED":
        return columns.get("done")
    by_label = {labels_cfg[key]: key for key in labels_cfg if key in _STATUS_KEYS}
    names = [label["name"] for label in issue["labels"]["nodes"]]
    for label in names:
        if label in by_label:
            key = by_label[label].replace("_", "-")
            return columns.get(key)
    return columns.get("refine")


_STATUS_KEYS = ("refine", "ready", "doing", "blocked_on_human", "ai_completed", "review")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    args = parser.parse_args()

    cfg = load_config()
    owner, name = cfg["repo"].split("/", 1)
    project_id, field_id, options = board(owner, int(cfg["board_number"]))
    columns, labels_cfg = cfg["board_columns"], cfg["labels"]

    for issue in issues(owner, name):
        column = wanted_column(issue, columns, labels_cfg)
        if not column:
            continue
        if column not in options:
            print(f"#{issue['number']}: no column '{column}' on the board")
            continue
        item = next(
            (i for i in issue["projectItems"]["nodes"] if i["project"]["id"] == project_id), None
        )
        current = ((item or {}).get("fieldValueByName") or {}).get("name")
        if item and current == column:
            continue
        print(f"#{issue['number']}: {current or ('not on board' if not item else 'no status')} -> {column}")
        if args.dry_run:
            continue
        if not item:
            added = gql(
                """mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){
                item{id}}}""",
                p=project_id,
                c=issue["id"],
            )
            item = added["addProjectV2ItemById"]["item"]
        gql(
            """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{
            projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}""",
            p=project_id,
            i=item["id"],
            f=field_id,
            o=options[column],
        )


if __name__ == "__main__":
    main()
