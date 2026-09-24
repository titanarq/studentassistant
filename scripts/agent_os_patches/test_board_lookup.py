"""Tests for the agent-os#27 workaround. No network: `gh` is faked, in-process (`issues._gh`) and,
for the end-to-end case, as an executable first on PATH. Run with the mechanism's interpreter:

    agent_os/.venv/bin/pytest scripts/agent_os_patches -q
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "agent_os"))
sys.path.insert(0, str(HERE))

import board_lookup  # noqa: E402
from agent_os import issues  # noqa: E402

ITEMS_ON_BOARD = {
    "data": {
        "repository": {
            "issue": {
                "projectItems": {
                    "nodes": [
                        {"id": "PVTI_other", "project": {"number": 3, "owner": {"login": "someone"}}},
                        {"id": "PVTI_4", "project": {"number": 4, "owner": {"login": "titanarq"}}},
                        {"id": "PVTI_3", "project": {"number": 3, "owner": {"login": "TitanArq"}}},
                    ]
                }
            }
        }
    }
}
STATUS_FIELD = {
    "data": {
        "repositoryOwner": {
            "projectV2": {
                "id": "PVT_3",
                "field": {"id": "F_status", "options": [{"id": "O1", "name": "Backlog"}]},
            }
        }
    }
}


def done(payload, code=0, stderr=""):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(returncode=code, stdout=text, stderr=stderr)


@pytest.fixture
def patched(monkeypatch):
    """`agent_os.issues` with the patch installed and `_gh` scripted; restores the originals."""
    calls: list[tuple] = []
    answers: list = []

    def fake_gh(*args, input_text=None):
        calls.append(args)
        return answers.pop(0)

    original_item, original_field = issues.board_item_id, issues.board_status_field
    monkeypatch.setattr(issues, "_gh", fake_gh)
    board_lookup.install(issues)
    yield SimpleNamespace(calls=calls, answers=answers)
    issues.board_item_id, issues.board_status_field = original_item, original_field
    del issues._agent_os_patches_board_lookup


def test_item_is_found_on_the_configured_board_only(patched):
    patched.answers.append(done(ITEMS_ON_BOARD))
    assert issues.board_item_id("titanarq", 3, "titanarq/studentassistant", 74) == "PVTI_3"
    (args,) = patched.calls
    assert args[:2] == ("api", "graphql")
    assert "number=74" in args and "owner=titanarq" in args and "name=studentassistant" in args
    assert "item-list" not in args


def test_an_issue_on_no_item_of_the_board_is_none_without_fallback(patched, monkeypatch):
    empty = {"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}
    patched.answers.append(done(empty))
    monkeypatch.setattr(board_lookup, "_original_item_id", lambda *a: pytest.fail("fell back"))
    assert issues.board_item_id("titanarq", 3, "titanarq/studentassistant", 74) is None


@pytest.mark.parametrize(
    "answer",
    [
        done("", code=1, stderr="HTTP 502"),
        done("not json"),
        done({"errors": [{"message": "boom"}]}),
        done({"data": {"repository": {"issue": None}}}),
    ],
)
def test_item_lookup_falls_back_to_the_original_on_any_error(patched, monkeypatch, answer):
    patched.answers.append(answer)
    monkeypatch.setattr(board_lookup, "_original_item_id", lambda *a: f"original{a}")
    got = issues.board_item_id("titanarq", 3, "titanarq/studentassistant", 74)
    assert got == "original('titanarq', 3, 'titanarq/studentassistant', 74)"


def test_status_field_is_read_by_name_in_one_query(patched):
    patched.answers.append(done(STATUS_FIELD))
    assert issues.board_status_field("titanarq", 3) == ("PVT_3", "F_status", {"Backlog": "O1"})
    (args,) = patched.calls
    assert args[:2] == ("api", "graphql") and "board=3" in args


def test_status_field_falls_back_when_the_board_has_no_status_field(patched, monkeypatch):
    no_field = {"data": {"repositoryOwner": {"projectV2": {"id": "PVT_3", "field": None}}}}
    patched.answers.append(done(no_field))
    monkeypatch.setattr(board_lookup, "_original_status_field", lambda *a: ("P", "F", {}))
    assert issues.board_status_field("titanarq", 3) == ("P", "F", {})


def test_mirror_board_column_uses_only_the_cheap_queries_then_edits(patched):
    patched.answers += [done(ITEMS_ON_BOARD), done(STATUS_FIELD), done("{}")]
    line = issues.mirror_board_column("titanarq/studentassistant", 74, "Backlog", 3)
    assert line == "board:    Backlog"
    assert [c[:2] for c in patched.calls] == [
        ("api", "graphql"),
        ("api", "graphql"),
        ("project", "item-edit"),
    ]
    edit = patched.calls[2]
    assert edit[edit.index("--id") + 1] == "PVTI_3"
    assert edit[edit.index("--single-select-option-id") + 1] == "O1"


FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["issue", "view"]:
        print(json.dumps({"labels": [{"name": "type:task"}, {"name": "status:refine"}],
                          "state": "OPEN", "title": "t", "url": "u"}))
    elif args[:2] == ["label", "list"]:
        print(json.dumps([{"name": "status:refine"}]))
    elif args[:1] == ["api"] and "graphql" not in args:
        print(json.dumps({"number": 74}))
    elif args[:2] == ["api", "graphql"] and any("projectItems" in a for a in args):
        print(json.dumps(%(items)s))
    elif args[:2] == ["api", "graphql"]:
        print(json.dumps(%(field)s))
    elif args[:2] == ["project", "item-edit"]:
        print("{}")
    else:
        sys.stderr.write("unexpected gh call: %%s\\n" %% args)
        sys.exit(1)
    """
) % {"items": repr(ITEMS_ON_BOARD), "field": repr(STATUS_FIELD)}


def test_the_wrapper_runs_issues_move_with_the_patch(tmp_path):
    """`scripts/agent_os_patches/python -m agent_os.issues move` -- what the guard, the drivers and
    the prompts run once AGENT_OS_PYTHON points at the wrapper -- never calls item-list/field-list."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(log),
        "AGENT_OS_HOST_ROOT": str(ROOT),
        "AGENT_OS_GH_REPO": "titanarq/studentassistant",
        "AGENT_OS_REAL_PYTHON": sys.executable,
    }
    env.pop("GH_TOKEN", None)
    out = subprocess.run(
        [str(HERE / "python"), "-m", "agent_os.issues", "move", "74", "refine"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "board:    Backlog" in out.stdout
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(c[:2] in (["project", "item-list"], ["project", "field-list"]) for c in calls)
    assert ["project", "item-edit"] == next(c[:2] for c in calls if c[0] == "project")


def test_the_wrapper_passes_everything_else_through(tmp_path):
    env = {**os.environ, "AGENT_OS_REAL_PYTHON": sys.executable}
    out = subprocess.run(
        [str(HERE / "python"), "-c", "import sys; print(sys.argv[1:])", "a", "b"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert out.returncode == 0 and out.stdout.strip() == "['a', 'b']"
