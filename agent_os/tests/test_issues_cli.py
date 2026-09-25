"""No-network tests for `agent_os.issues`: the backlog YAML -> GitHub Issue mapping, the
fixed labels and the repository, both read from `config/agents.yaml`, the key-line parser, and the
`gh` subprocess plumbing (mocked — never actually shells out)."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.parse
from unittest.mock import patch

import pytest

from agent_os import issues
from agent_os.cli import AGENT_OS_DIR
from agent_os.lib import (
    REQUIRED_SECTIONS,
    HumanMessageError,
    LabelVocabulary,
    ProjectConfig,
    load_project,
    render_human_message,
    replace_blocker,
)

# --------------------------------------------------------------------------------------------
# YAML/ADO entry -> GitHub mapping
# --------------------------------------------------------------------------------------------


def test_type_label_epic_and_task():
    assert issues.type_label({"type": "Epic"}) == "type:epic"
    assert issues.type_label({"type": "Task"}) == "type:task"


def test_type_label_issue_feature_vs_bug():
    assert issues.type_label({"type": "Issue", "tags": ["fase-3"]}) == "type:feature"
    assert issues.type_label({"type": "Issue", "tags": ["fase-3", "bug"]}) == "type:bug"


def test_desired_labels_priority_status_and_tags():
    entry = {"type": "Issue", "state": "Doing", "priority": 2, "tags": ["fase-4", "opus", "bug"]}
    labels = issues.desired_labels(entry)
    # "bug" flips the type label to type:bug AND is still carried over verbatim as its own tag.
    assert labels == ["type:bug", "status:doing", "p2", "fase-4", "opus", "bug"]


def test_desired_labels_spells_doing_the_way_the_label_vocabulary_does():
    # The one spelling `move` writes, not a second copy of it: a project renaming its in-progress
    # label in config must not end up with two of them on the tracker.
    project = _project(labels=LabelVocabulary(doing="wip"))
    assert issues.desired_labels({"type": "Task", "state": "Doing"}, project) == [
        "type:task",
        "wip",
    ]


def test_desired_labels_never_includes_key_tags():
    entry = {"type": "Epic", "tags": ["key-fase-3", "fase-3"]}
    assert "key-fase-3" not in issues.desired_labels(entry)
    assert "fase-3" in issues.desired_labels(entry)


def test_desired_labels_deduplicates_preserving_order():
    entry = {"type": "Epic", "priority": 1, "tags": ["p1"]}
    # "p1" would be added both by priority and by the (contrived) tag; must appear once.
    assert issues.desired_labels(entry).count("p1") == 1


@pytest.mark.parametrize(
    "state,expected", [("Done", "closed"), ("Doing", "open"), ("To Do", "open")]
)
def test_desired_gh_state(state, expected):
    assert issues.desired_gh_state({"state": state}) == expected


def test_desired_gh_state_defaults_to_open():
    assert issues.desired_gh_state({}) == "open"


def test_flatten_parent_before_child():
    document = {
        "items": [
            {
                "key": "fase-4",
                "type": "Epic",
                "title": "Fase 4",
                "children": [
                    {
                        "key": "bat-c-01",
                        "type": "Issue",
                        "title": "C-01",
                        "children": [
                            {"key": "bat-c-01a", "type": "Task", "title": "C-01a"},
                        ],
                    },
                ],
            },
        ],
    }
    entries = issues.flatten(document["items"])
    index = {entry["key"]: position for position, entry in enumerate(entries)}
    assert index["fase-4"] < index["bat-c-01"] < index["bat-c-01a"]
    assert entries[index["bat-c-01"]]["parent"] == "fase-4"
    assert entries[index["bat-c-01a"]]["parent"] == "bat-c-01"


# --------------------------------------------------------------------------------------------
# key line: compose / parse
# --------------------------------------------------------------------------------------------


def test_compose_body_key_line_is_last():
    body = issues.compose_body({"description": "Some text.", "target_date": "2026-09-30"}, "fase-3")
    lines = body.splitlines()
    assert lines[-1] == "<!-- key: fase-3 -->"
    assert "Target date: 2026-09-30" in body
    assert issues.parse_key_line(body) == "fase-3"


def test_compose_body_empty_description_still_has_key_line():
    body = issues.compose_body({}, "cp-3.16")
    assert body == "<!-- key: cp-3.16 -->"
    assert issues.parse_key_line(body) == "cp-3.16"


def test_parse_key_line_missing():
    assert issues.parse_key_line("just a body, no key comment") is None
    assert issues.parse_key_line("") is None
    assert issues.parse_key_line(None) is None


def test_parse_key_line_takes_the_last_match():
    body = "<!-- key: stale-one -->\n\nSome edit happened.\n\n<!-- key: current-key -->"
    assert issues.parse_key_line(body) == "current-key"


# --------------------------------------------------------------------------------------------
# sub-issues (#343): `gh issue view --json subIssues` returns the GraphQL connection, not a list
# --------------------------------------------------------------------------------------------


def test_sub_issue_numbers_reads_the_gh_connection_shape():
    # Verbatim shape of `gh issue view 336 --json subIssues` (2026-09-14): a dict, not a list.
    connection = {
        "nodes": [
            {"id": "I_kwA", "number": 341, "state": "CLOSED", "title": "one"},
            {"id": "I_kwB", "number": 342, "state": "OPEN", "title": "two"},
        ],
        "totalCount": 2,
    }
    assert issues.sub_issue_numbers(connection) == [341, 342]


def test_sub_issue_numbers_also_reads_a_plain_list():
    # What the REST sub-issues endpoint returns, and what cmd_show used to assume was the only
    # shape -- the assumption that made `show` crash on any issue with children (#343).
    assert issues.sub_issue_numbers([{"number": 7}, {"number": 8}]) == [7, 8]


def test_sub_issue_numbers_empty_for_an_issue_with_no_children():
    assert issues.sub_issue_numbers(None) == []
    assert issues.sub_issue_numbers({"nodes": [], "totalCount": 0}) == []
    assert issues.sub_issue_numbers("unexpected") == []


def test_cmd_show_prints_children_for_the_gh_connection_shape(capsys):
    data = {
        "number": 336,
        "title": "parent",
        "state": "OPEN",
        "labels": [],
        "body": "",
        "parent": {"number": 12},
        "subIssues": {"nodes": [{"number": 341}, {"number": 346}], "totalCount": 2},
        "url": "https://example.invalid/336",
    }
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=data),
    ):
        issues.cmd_show(argparse.Namespace(number=336))
    out = capsys.readouterr().out
    assert "children #341, #346" in out
    assert "parent   #12" in out


# --------------------------------------------------------------------------------------------
# gh subprocess plumbing (mocked)
# --------------------------------------------------------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _project(**overrides):
    """A `ProjectConfig` that is nobody's project: the tests that assert where a value comes from
    must not be able to pass by reading the host's own `config/agents.yaml`."""
    fields = {"repo": "", "tracking_epic": 1, "board_number": 1, "modules": []}
    return ProjectConfig(**(fields | overrides))


def test_repo_name_prefers_the_env_variable_over_the_config_and_never_shells_out(monkeypatch):
    monkeypatch.setenv("AGENT_OS_GH_REPO", "other-owner/other-name")
    with (
        patch.object(issues, "load_project", return_value=_project(repo="owner/name")),
        patch("agent_os.issues.subprocess.run") as run,
    ):
        assert issues.repo_name() == "other-owner/other-name"
        run.assert_not_called()


def test_repo_name_reads_project_repo_from_the_config_when_no_variable_is_set(monkeypatch):
    # The whole point of gap (d): `project.repo` was declared and never read, so a command run
    # from outside the clone pointed at whatever repository the cwd happened to be.
    monkeypatch.delenv("AGENT_OS_GH_REPO", raising=False)
    with (
        patch.object(issues, "load_project", return_value=_project(repo="owner/name")),
        patch("agent_os.issues.subprocess.run") as run,
    ):
        assert issues.repo_name() == "owner/name"
        run.assert_not_called()


def test_repo_name_falls_back_to_gh_repo_view_only_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("AGENT_OS_GH_REPO", raising=False)
    with (
        patch.object(issues, "load_project", return_value=_project(repo="")),
        patch(
            "agent_os.issues.subprocess.run", return_value=_completed(stdout="owner/name\n")
        ) as run,
    ):
        assert issues.repo_name() == "owner/name"
        assert run.call_args[0][0][:2] == ["gh", "repo"]


def test_the_real_config_names_the_repository_this_tracker_runs_against(monkeypatch):
    monkeypatch.delenv("AGENT_OS_GH_REPO", raising=False)
    with patch("agent_os.issues.subprocess.run") as run:
        assert issues.repo_name() == load_project().repo
        run.assert_not_called()


# ---- fixed labels ------------------------------------------------------------------------------


def test_fixed_labels_take_their_modules_from_the_config_never_from_the_code():
    labels = issues.fixed_labels(_project(modules=["alpha", "beta"]))
    assert [label for label in labels if label.startswith("module:")] == [
        "module:alpha",
        "module:beta",
    ]
    # The type and priority halves are the mechanism's own vocabulary, the same in every project.
    assert labels[:4] == ["type:epic", "type:feature", "type:task", "type:bug"]
    assert "p4" in labels


def test_fixed_labels_take_the_in_progress_label_from_the_configured_vocabulary():
    # `project.labels.doing` exists on the same object the modules come from; a literal beside it
    # would be exactly the drift `LabelVocabulary` was written to stop.
    assert "wip" in issues.fixed_labels(_project(labels=LabelVocabulary(doing="wip")))
    assert "status:doing" not in issues.fixed_labels(_project(labels=LabelVocabulary(doing="wip")))
    assert load_project().labels.doing in issues.fixed_labels()


def test_fixed_labels_of_a_project_with_no_modules_carry_no_module_label():
    assert not [
        label for label in issues.fixed_labels(_project(modules=[])) if label.startswith("module:")
    ]


def test_gh_json_parses_success():
    with patch("agent_os.issues.subprocess.run", return_value=_completed(stdout='{"a": 1}')):
        assert issues.gh_json("issue", "view", "1") == {"a": 1}


def test_gh_json_returns_none_for_empty_stdout():
    with patch("agent_os.issues.subprocess.run", return_value=_completed(stdout="")):
        assert issues.gh_json("label", "create", "x") is None


# The exact text `gh` printed on a host under concurrent load (#70). It is GitHub's answer when the
# GraphQL bucket of the login is empty -- a quota separate from the REST `core` one that the
# top-level `gh api rate_limit` `.rate` reports, which is why that read `remaining: 5000` at the
# same moment.
GRAPHQL_EXHAUSTED = "GraphQL: API rate limit already exceeded for user ID 1234567."


def _rate_limit_resources(*, graphql_remaining: int, core_remaining: int = 5000) -> str:
    """What `gh api rate_limit --jq .resources` prints: one bucket per API, each its own quota."""
    return json.dumps(
        {
            "core": {"limit": 5000, "remaining": core_remaining, "reset": 1790320000},
            "graphql": {"limit": 5000, "remaining": graphql_remaining, "reset": 1790319600},
            "search": {"limit": 30, "remaining": 30, "reset": 1790316060},
        }
    )


def _fake_gh(failures: list[str], *, resources: str | None = None, stdout: str = '{"ok": true}'):
    """A `subprocess.run` for `gh`: `gh api rate_limit` answers `resources` (fails when None);
    every other command fails once per entry of `failures`, with that stderr, then succeeds."""
    calls: list[list[str]] = []
    pending = list(failures)

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "api", "rate_limit"]:
            if resources is None:
                return _completed(returncode=1, stderr="HTTP 502")
            return _completed(stdout=resources)
        if pending:
            return _completed(returncode=1, stderr=pending.pop(0))
        return _completed(stdout=stdout)

    return run, calls


def test_gh_json_retries_on_rate_limit_then_succeeds():
    run, calls = _fake_gh(
        ["HTTP 403: API rate limit exceeded"], resources=_rate_limit_resources(graphql_remaining=10)
    )
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
    ):
        assert issues.gh_json("issue", "list") == {"ok": True}
    assert [call for call in calls if call[:3] != ["gh", "api", "rate_limit"]] == [
        ["gh", "issue", "list"],
        ["gh", "issue", "list"],
    ]
    sleep.assert_called_once()


def test_an_exhausted_graphql_quota_fails_at_once_naming_the_bucket_and_its_reset():
    # #70: the host saw this error while REST kept working and read it as a false positive; the
    # old loop slept 1+2+4+8+16 s against a quota that resets up to an hour later and then failed
    # with gh's text alone. The failure now says which quota is empty and when it refills.
    run, calls = _fake_gh(
        [GRAPHQL_EXHAUSTED] * 6, resources=_rate_limit_resources(graphql_remaining=0)
    )
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
        pytest.raises(SystemExit) as exited,
    ):
        issues.gh_json("issue", "view", "1", "--json", "body")
    message = str(exited.value.code)
    assert "graphql: 0 of 5000 left, resets at 2026-09-2" in message
    assert "core:" not in message  # the REST bucket, full, is not blamed
    assert GRAPHQL_EXHAUSTED in message
    sleep.assert_not_called()
    assert calls.count(["gh", "issue", "view", "1", "--json", "body"]) == 1


def test_a_rate_limit_with_quota_left_is_transient_and_retried():
    run, _ = _fake_gh([GRAPHQL_EXHAUSTED], resources=_rate_limit_resources(graphql_remaining=4000))
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
    ):
        assert issues.gh_json("issue", "list") == {"ok": True}
    sleep.assert_called_once()


def test_a_rate_limit_whose_quota_cannot_be_read_is_still_retried():
    run, _ = _fake_gh([GRAPHQL_EXHAUSTED], resources=None)
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
    ):
        assert issues.gh_json("issue", "list") == {"ok": True}
    sleep.assert_called_once()


def test_a_secondary_rate_limit_waits_at_least_the_minute_github_asks_for():
    secondary = (
        "You have exceeded a secondary rate limit. Please wait a few minutes before you try "
        "again. (HTTP 403)"
    )
    run, calls = _fake_gh([secondary], resources=_rate_limit_resources(graphql_remaining=4000))
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
    ):
        assert issues.gh_json("issue", "list") == {"ok": True}
    assert sleep.call_args.args[0] >= 60
    # A secondary limit is not a quota: reading the buckets would answer nothing about it.
    assert not [call for call in calls if call[:3] == ["gh", "api", "rate_limit"]]


def test_a_403_that_is_not_a_rate_limit_fails_without_retrying():
    run, calls = _fake_gh(["HTTP 403: Resource not accessible by integration"])
    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep") as sleep,
        pytest.raises(SystemExit),
    ):
        issues.gh_json("issue", "list")
    sleep.assert_not_called()
    assert len(calls) == 1


def test_gh_json_exits_on_non_rate_limit_failure():
    with (
        patch(
            "agent_os.issues.subprocess.run",
            return_value=_completed(returncode=1, stderr="422 Validation failed"),
        ),
        pytest.raises(SystemExit),
    ):
        issues.gh_json("issue", "create")


def test_ensure_labels_creates_only_missing(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _completed(returncode=0, stdout="")

    with (
        patch("agent_os.issues.subprocess.run", side_effect=fake_run),
        patch("agent_os.issues.time.sleep"),
    ):
        cache = {"type:epic"}
        issues.ensure_labels("owner/repo", ["type:epic", "type:feature", "p1"], cache)

    created = [call for call in calls if call[:2] == ["gh", "label"]]
    created_names = [call[3] for call in created]
    assert created_names == ["type:feature", "p1"]
    assert cache == {"type:epic", "type:feature", "p1"}


def test_add_sub_issue_uses_plural_endpoint_and_typed_field():
    with (
        patch("agent_os.issues.subprocess.run", return_value=_completed(stdout="{}")) as run,
        patch("agent_os.issues.time.sleep"),
    ):
        issues.add_sub_issue("owner/repo", 10, 999)
    args = run.call_args[0][0]
    assert args[:2] == ["gh", "api"]
    assert args[2] == "repos/owner/repo/issues/10/sub_issues"
    assert "-X" in args and args[args.index("-X") + 1] == "POST"
    assert "-F" in args and args[args.index("-F") + 1] == "sub_issue_id=999"


def test_remove_sub_issue_uses_singular_endpoint_and_delete():
    with (
        patch("agent_os.issues.subprocess.run", return_value=_completed(stdout="{}")) as run,
        patch("agent_os.issues.time.sleep"),
    ):
        issues.remove_sub_issue("owner/repo", 10, 999)
    args = run.call_args[0][0]
    assert args[2] == "repos/owner/repo/issues/10/sub_issue"
    assert "-X" in args and args[args.index("-X") + 1] == "DELETE"
    assert "-F" in args and args[args.index("-F") + 1] == "sub_issue_id=999"


def test_create_issue_sends_labels_as_array_fields():
    with (
        patch(
            "agent_os.issues.subprocess.run",
            return_value=_completed(stdout='{"number": 1, "id": 2}'),
        ) as run,
        patch("agent_os.issues.time.sleep"),
    ):
        result = issues.create_issue("owner/repo", "Title", "Body", ["type:epic", "fase-3"])
    args = run.call_args[0][0]
    assert result == {"number": 1, "id": 2}
    assert "-f" in args and "labels[]=type:epic" in args and "labels[]=fase-3" in args


def test_find_by_key_matches_exact_key_only():
    rows = [
        {"number": 1, "body": "text\n\n<!-- key: fase-3-old -->"},
        {"number": 2, "body": "text\n\n<!-- key: fase-3 -->"},
    ]
    with patch("agent_os.issues.gh_json", return_value=rows):
        assert issues.find_by_key("owner/repo", "fase-3") == 2


def test_find_by_key_returns_none_when_absent():
    with patch("agent_os.issues.gh_json", return_value=[]):
        assert issues.find_by_key("owner/repo", "missing-key") is None


def test_sync_entry_dry_run_never_calls_gh():
    entry = {"key": "fase-3", "type": "Epic", "title": "Fase 3", "state": "Doing"}
    with patch("agent_os.issues.subprocess.run") as run:
        verb = issues.sync_entry("owner/repo", entry, {}, set(), dry_run=True)
    run.assert_not_called()
    assert verb is None


# --------------------------------------------------------------------------------------------
# Templates, validate, move, brief
# --------------------------------------------------------------------------------------------
# agent_os/docs/adr/2026-09-14-the-issue-is-the-unit-of-work-and-status-labels-are-the-mechanical-state.md

VALID_BODY = (
    "\n\n".join(f"{heading}\nsomething" for heading in REQUIRED_SECTIONS).replace(
        "## Stages\nsomething", "## Stages\n- [ ] Do the thing"
    )
    + "\n\n<!-- budget: mechanical-qwen -->"
)


@pytest.fixture
def shipped_issue_templates(tmp_path, monkeypatch):
    """`issues.template_body()` reads `TEMPLATE_DIR / f"{name}.md"`, a HOST path
    (`.github/ISSUE_TEMPLATE/`) that does not exist outside a checkout that ships one. The mechanism
    ships its own copies for exactly this (`agent_os/templates/issue_template/`, also what
    `agent_os.install.plan_issue_templates` writes into a fresh host, #511) -- this fixture copies
    those into a throwaway directory and points `issues.TEMPLATE_DIR` at it, so `cmd_create`'s
    `--template` path is exercised against the mechanism's own scaffold rather than any host's."""
    template_dir = tmp_path / "ISSUE_TEMPLATE"
    template_dir.mkdir()
    source_dir = AGENT_OS_DIR / "templates" / "issue_template"
    for name in ("task", "bug"):
        shutil.copy(source_dir / f"{name}.md", template_dir / f"{name}.md")
    monkeypatch.setattr(issues, "TEMPLATE_DIR", template_dir)
    return template_dir


def test_create_with_a_template_and_no_title_prints_the_scaffold_and_creates_nothing(
    capsys, shipped_issue_templates
):
    with patch("agent_os.issues.subprocess.run") as run:
        issues.cmd_create(
            argparse.Namespace(
                type=None,
                title=None,
                parent=None,
                body_file=None,
                template="task",
                label=None,
            )
        )
        run.assert_not_called()
    assert "## Acceptance criteria" in capsys.readouterr().out


def test_create_refuses_a_template_and_a_body_file_at_once():
    with pytest.raises(SystemExit):
        issues.cmd_create(
            argparse.Namespace(
                type=None,
                title="t",
                parent=None,
                body_file="x.md",
                template="task",
                label=None,
            )
        )


def _created_title(title, *, issue_type="task", template=None):
    """The title `cmd_create` sends to GitHub, every `gh` call mocked."""
    created = {"number": 9, "id": 99, "html_url": "u"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "ensure_fixed_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "create_issue", return_value=created) as create,
        patch.object(issues, "add_to_board", return_value=[]),
    ):
        issues.cmd_create(
            argparse.Namespace(
                type=None if template else issue_type,
                title=title,
                parent=None,
                body_file=None,
                template=template,
                label=None,
            )
        )
    return create.call_args[0][1]


@pytest.mark.parametrize(
    "issue_type,prefixed", [("task", "[task] Split it"), ("bug", "[bug] Split it")]
)
def test_create_prefixes_the_title_the_types_template_declares(
    shipped_issue_templates, issue_type, prefixed
):
    """#15: the refiner's `create --type task --title T` produced `T`, not the `[task] T` the
    template's front matter declares and every hand-written task carries."""
    assert _created_title("Split it", issue_type=issue_type) == prefixed


def test_create_from_a_template_also_prefixes_the_title(shipped_issue_templates):
    assert _created_title("Split it", template="task") == "[task] Split it"


@pytest.mark.parametrize("title", ["[task] Split it", "[task]Split it", "[Task] Split it"])
def test_create_never_prefixes_a_title_that_already_carries_the_prefix(
    shipped_issue_templates, title
):
    assert _created_title(title) == title


def test_prefixing_twice_is_prefixing_once(shipped_issue_templates):
    assert _created_title(_created_title("Split it")) == "[task] Split it"


def test_the_prefix_comes_from_the_hosts_template_never_from_the_code(shipped_issue_templates):
    task = shipped_issue_templates / "task.md"
    task.write_text(task.read_text().replace("title: '[task] '", "title: 'TASK: '"))
    assert _created_title("Split it") == "TASK: Split it"


def test_a_type_with_no_template_or_no_title_in_it_keeps_its_title(shipped_issue_templates):
    # `epic` and `feature` have no template; a template may declare no `title:` at all.
    assert _created_title("Group them", issue_type="epic") == "Group them"
    bug = shipped_issue_templates / "bug.md"
    bug.write_text(bug.read_text().replace("title: '[bug] '\n", ""))
    assert _created_title("Split it", issue_type="bug") == "Split it"


def test_create_from_a_template_sends_the_scaffold_as_the_body_and_infers_the_type(
    shipped_issue_templates,
):
    created = {"number": 9, "id": 99, "html_url": "u"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "ensure_fixed_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "create_issue", return_value=created) as create,
        patch.object(issues, "add_to_board", return_value=[]),
    ):
        issues.cmd_create(
            argparse.Namespace(
                type=None,
                title="t",
                parent=None,
                body_file=None,
                template="bug",
                label=None,
            )
        )
    _repo, _title, body, labels = create.call_args[0]
    assert labels == ["type:bug"]
    assert "## Definition of done" in body


# ---- create puts the new issue on the board (#23) --------------------------------------------


def _create_on_board(labels=None, *, board=5, item_add=None, mirror=None):
    """Runs `cmd_create` with every `gh` call mocked and `project.board_number = board`. Returns
    `(gh_calls, mirror_calls)`: every `gh_json` call made (the board's `item-add` among them) and
    the arguments `mirror_board_column` was called with. `item_add` answers the `item-add`, or
    raises when it is an exception -- `gh_json` exits on a failed `gh`."""
    project = _project(
        board_number=board, board_columns={"ready": "Ready for AI", "refine": "Backlog"}
    )
    created = {"number": 9, "id": 99, "html_url": "https://github.invalid/owner/name/issues/9"}
    calls = []

    def fake_gh(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("project", "item-add"):
            if isinstance(item_add, BaseException):
                raise item_add
            return item_add if item_add is not None else {"id": "PVTI_new"}
        raise AssertionError(f"unexpected gh call: {args}")

    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "load_project", return_value=project),
        patch.object(issues, "ensure_fixed_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "create_issue", return_value=created),
        patch.object(issues, "gh_json", side_effect=fake_gh),
        patch.object(
            issues, "mirror_board_column", side_effect=mirror, return_value="board:    mirrored"
        ) as board_column,
    ):
        issues.cmd_create(
            argparse.Namespace(
                type="task",
                title="t",
                parent=None,
                body_file=None,
                template=None,
                label=labels,
            )
        )
    return calls, board_column.call_args_list


def test_create_adds_the_new_issue_to_the_configured_board():
    calls, _ = _create_on_board()
    (item_add,) = [args for args in calls if args[:2] == ("project", "item-add")]
    assert item_add[2] == "5"
    assert item_add[item_add.index("--owner") + 1] == "owner"
    assert item_add[item_add.index("--url") + 1] == "https://github.invalid/owner/name/issues/9"


def test_create_sets_the_column_of_its_initial_status_label_on_the_new_item(capsys):
    _, mirrored = _create_on_board(["status:ready"])
    (call,) = mirrored
    assert call.args == ("owner/name", 9, "Ready for AI", 5)
    # The item `item-add` just returned, not a lookup that may not see it yet.
    assert call.kwargs == {"item": "PVTI_new"}
    assert "board:    mirrored" in capsys.readouterr().out


def test_create_without_a_status_label_adds_the_item_and_leaves_its_column_alone(capsys):
    calls, mirrored = _create_on_board(["p2"])
    assert [args[:2] for args in calls] == [("project", "item-add")]
    assert mirrored == []
    assert "board:    added #9 to project 5" in capsys.readouterr().out


def test_a_board_that_refuses_the_item_never_fails_the_create(capsys):
    _, mirrored = _create_on_board(
        ["status:ready"], item_add=SystemExit("gh project item-add failed:\nno such project")
    )
    out = capsys.readouterr().out
    assert "created #9" in out
    assert "board:    #9 not added to project 5" in out
    assert mirrored == []


def test_a_column_that_cannot_be_set_never_fails_the_create(capsys):
    _, mirrored = _create_on_board(
        ["status:ready"], mirror=SystemExit("project owner/5 has no single-select field")
    )
    out = capsys.readouterr().out
    assert len(mirrored) == 1
    assert "board:    column not mirrored: project owner/5 has no single-select field" in out


def test_create_with_no_board_configured_makes_no_board_call():
    calls, mirrored = _create_on_board(["status:ready"], board=0)
    assert calls == [] and mirrored == []


def test_mirror_board_column_uses_the_item_it_is_given_without_looking_it_up():
    with (
        patch.object(issues, "board_item_id") as lookup,
        patch.object(issues, "board_status_field", return_value=("PVT", "F2", {"Ready": "O1"})),
        patch.object(issues, "gh_json") as edit,
    ):
        line = issues.mirror_board_column("owner/name", 9, "Ready", 5, item="PVTI_new")
    lookup.assert_not_called()
    args = edit.call_args[0]
    assert args[args.index("--id") + 1] == "PVTI_new"
    assert line == "board:    Ready"


# ---- update --body-file: the refiner's own way to rewrite a body in place --------------------


def test_update_with_body_file_replaces_the_body(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text("new body text")
    current = {"labels": [], "title": "t"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "gh_json"),
    ):
        issues.cmd_update(
            argparse.Namespace(
                number=7,
                state=None,
                comment=None,
                add_label=None,
                remove_label=None,
                title=None,
                body_file=str(body_file),
            )
        )
    fields = update.call_args[0][2]
    assert fields == {"body": "new body text"}


def test_update_with_body_file_and_labels_combines_both_fields(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text("new body")
    current = {"labels": [{"name": "status:refine"}], "title": "t"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "ensure_fixed_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "gh_json"),
    ):
        issues.cmd_update(
            argparse.Namespace(
                number=7,
                state=None,
                comment=None,
                add_label=["status:ready"],
                remove_label=None,
                title=None,
                body_file=str(body_file),
            )
        )
    fields = update.call_args[0][2]
    assert fields["body"] == "new body"
    assert fields["labels"] == ["status:refine", "status:ready"]


def test_update_without_body_file_leaves_the_body_untouched():
    current = {"labels": [], "title": "t"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "gh_json"),
    ):
        issues.cmd_update(
            argparse.Namespace(
                number=7,
                state=None,
                comment="hi",
                add_label=None,
                remove_label=None,
                title=None,
                body_file=None,
            )
        )
    update.assert_not_called()  # a comment alone never PATCHes the issue


# ---- validate --------------------------------------------------------------------------------


def _validate(body, *, open_numbers=()):
    """`validate_issue` with the issue's REST read mocked to return `body` and the open-issue
    listing to return `open_numbers`. Never shells out."""
    with (
        patch.object(issues, "gh_json_dict", return_value={"body": body}),
        patch.object(issues, "gh_text", return_value="\n".join(map(str, open_numbers))),
    ):
        return issues.validate_issue("owner/name", 1)


def test_validate_reads_over_rest_and_works_with_the_graphql_quota_exhausted():
    # #70: `validate` is what `worker_task.sh start` runs before every dispatch, and it was
    # GraphQL-only (`gh issue view --json`, `gh issue list --json`); a host whose GraphQL bucket
    # was empty could not dispatch at all while REST had its whole quota left.
    blocked = VALID_BODY.replace("## Dependencies\nsomething", "## Dependencies\nBlocked by #40")
    issue = {"body": blocked, "labels": [{"name": "type:task"}], "number": 1}

    def run(args, **kwargs):
        if args[:2] in (["gh", "issue"], ["gh", "pr"], ["gh", "label"]) or "graphql" in args:
            return _completed(returncode=1, stderr=GRAPHQL_EXHAUSTED)
        if args[:3] == ["gh", "api", "repos/owner/name/issues/1"]:
            return _completed(stdout=json.dumps(issue))
        if args[:2] == ["gh", "api"] and args[2].startswith("repos/owner/name/issues?"):
            assert "--paginate" in args
            return _completed(stdout="40\n41\n")
        return _completed(returncode=1, stderr=f"unexpected {args}")

    with (
        patch("agent_os.issues.subprocess.run", side_effect=run),
        patch("agent_os.issues.time.sleep"),
    ):
        assert issues.validate_issue("owner/name", 1) == ["blocked by #40, which is still open"]


def test_the_open_issue_listing_leaves_pull_requests_out():
    # REST's issue listing also returns every open pull request; a `Blocked by #N` naming a PR is
    # not an open issue, exactly as `gh issue list` never listed one.
    with patch.object(issues, "gh_text", return_value="3\n7\n") as listing:
        assert issues.open_issue_numbers("owner/name") == {3, 7}
    args = listing.call_args.args
    assert args[1].startswith("repos/owner/name/issues?state=open")
    assert "select(.pull_request == null)" in args[args.index("--jq") + 1]


def test_validate_issue_passes_on_a_template_shaped_body():
    assert _validate(VALID_BODY) == []


def test_validate_issue_reports_a_missing_section():
    assert _validate(VALID_BODY.replace("## Context\nsomething", "")) == [
        "missing section: ## Context"
    ]


def test_validate_issue_reports_an_unresolvable_budget_class():
    failures = _validate(VALID_BODY.replace("mechanical-qwen", "invented"))
    assert failures == [
        failure for failure in failures if failure.startswith("budget class 'invented'")
    ]
    assert len(failures) == 1


def test_validate_issue_reports_an_open_blocker_and_accepts_a_closed_one():
    blocked = VALID_BODY.replace("## Dependencies\nsomething", "## Dependencies\nBlocked by #40")
    assert _validate(blocked, open_numbers=[40]) == ["blocked by #40, which is still open"]
    assert _validate(blocked, open_numbers=[41]) == []


def test_validate_issue_skips_the_open_listing_when_nothing_blocks():
    with (
        patch.object(issues, "gh_json_dict", return_value={"body": VALID_BODY}),
        patch.object(issues, "gh_text") as listing,
    ):
        assert issues.validate_issue("owner/name", 1) == []
        listing.assert_not_called()


@pytest.mark.parametrize("type_label", ["type:feature", "type:epic"])
def test_validate_issue_refuses_a_feature_or_an_epic_as_not_a_brief(type_label):
    view = {
        "body": "a grouping body with none of the brief sections",
        "labels": [{"name": type_label}],
    }
    with (
        patch.object(issues, "gh_json_dict", return_value=view),
        patch.object(issues, "gh_text") as listing,
    ):
        assert issues.validate_issue("owner/name", 336) == [
            f"#336 is {type_label}, not a brief: only type:task and type:bug issues are validated"
        ]
        listing.assert_not_called()


@pytest.mark.parametrize("type_label", ["type:task", "type:bug"])
def test_validate_issue_still_checks_the_sections_of_a_task_or_a_bug(type_label):
    view = {
        "body": VALID_BODY.replace("## Context\nsomething", ""),
        "labels": [{"name": type_label}],
    }
    with patch.object(issues, "gh_json_dict", return_value=view):
        assert issues.validate_issue("owner/name", 1) == ["missing section: ## Context"]


def test_cmd_validate_exits_non_zero_and_prints_one_line_per_failure(capsys):
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "validate_issue", return_value=["a", "b"]),
        pytest.raises(SystemExit) as exit_info,
    ):
        issues.cmd_validate(argparse.Namespace(number=1))
    assert exit_info.value.code == 1
    assert capsys.readouterr().out.splitlines()[-2:] == ["a", "b"]


def test_cmd_validate_prints_ok_and_returns_on_a_clean_issue(capsys):
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "validate_issue", return_value=[]),
    ):
        issues.cmd_validate(argparse.Namespace(number=1))
    assert capsys.readouterr().out.splitlines()[-1] == "ok"


# ---- move ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def review_pages_in_a_temporary_directory(tmp_path, monkeypatch):
    """`move N review` pages once per issue and records that in `.cache/paged-review/<N>`. Every
    test in this file gets its own, so no run of the suite can page for real or inherit a marker
    written by another test."""
    monkeypatch.setattr(issues, "REVIEW_PAGES_DIR", tmp_path / "paged-review")


@pytest.fixture(autouse=True)
def board_field_asked_afresh():
    """`board_status_field` answers once per process (#27); a test's board must never be the one
    a previous test's fake answered."""
    issues.board_status_field.cache_clear()
    yield
    issues.board_status_field.cache_clear()


def _move(state, held_labels, *, issue_state="OPEN", pages=None, title="A title"):
    """Runs `cmd_move` with every `gh` call mocked. Returns `(fields, board_line)`: what would
    have been PATCHed onto the issue, and what the board mirror printed. `pages` collects whatever
    reached `agent_os/bin/notify.sh`; the marker directory is redirected per call so "once per issue"
    is exercised deliberately, never inherited from a previous test."""
    current = {
        "labels": [{"name": name} for name in held_labels],
        "state": issue_state,
        "title": title,
        "html_url": "https://github.invalid/owner/name/issues/7",
    }
    sent = pages if pages is not None else []
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "label_exists", return_value=False),
        patch.object(issues, "ensure_labels") as ensure,
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "mirror_board_column", return_value="board:    mirrored") as board,
        patch.object(
            issues, "page_human", side_effect=lambda message: sent.append(message) is None
        ),
    ):
        issues.cmd_move(argparse.Namespace(numbers=[7], state=state))
    return update.call_args[0][2], board.call_args, ensure.call_args


def test_move_leaves_exactly_one_state_label_and_keeps_the_others():
    fields, _, _ = _move("doing", ["type:task", "module:workers", "status:ready"])
    assert fields["labels"] == ["type:task", "module:workers", "status:doing"]


def test_move_never_strips_the_human_only_full_stop():
    # `status:agents-paused` is not a state: it is the human's full stop and `move` must not
    # touch it, whichever state it is moving to.
    fields, _, _ = _move("review", ["status:agents-paused", "status:doing"])
    assert fields["labels"] == ["status:agents-paused", "status:review"]


def test_move_creates_the_target_label_if_it_is_missing():
    _, _, ensure = _move("ai-completed", [])
    assert ensure[0][1] == ["status:ai-completed"]


def test_move_to_done_removes_every_state_label_and_closes_the_issue():
    fields, _, _ = _move("done", ["type:task", "status:review"])
    assert fields == {"labels": ["type:task"], "state": "closed"}


def test_move_to_done_on_an_already_closed_issue_does_not_reclose_it():
    fields, _, _ = _move("done", ["status:review"], issue_state="CLOSED")
    assert fields == {"labels": []}


@pytest.mark.parametrize(
    "state,column",
    [
        ("refine", "Backlog"),
        ("ready", "Ready for AI"),
        ("doing", "In progress"),
        ("ai-completed", "AI completed"),
        ("review", "Review"),
        ("done", "Done"),
    ],
)
def test_move_mirrors_the_column_named_in_the_config_never_one_in_the_code(state, column):
    # The mapping is config, not code: this asserts the value the config actually holds today,
    # and reads it from there, so renaming a column in config/agents.yaml is a config change.
    assert load_project().board_columns[state] == column
    _, board, _ = _move(state, [])
    assert board[0][2] == column


def test_move_to_blocked_on_human_keeps_the_items_current_column(capsys):
    _, board, _ = _move("blocked-on-human", [])
    assert board is None
    assert "keeps the item's current column" in capsys.readouterr().out


def test_move_to_review_pages_the_human_with_the_configured_template(capsys):
    pages = []
    _move("review", ["type:task"], pages=pages, title="A title")
    assert pages == [
        render_human_message(
            "review_ready",
            issue=7,
            title="A title",
            url="https://github.invalid/owner/name/issues/7",
        )
    ]
    assert pages[0] in capsys.readouterr().out


def test_move_to_review_pages_once_per_issue_however_often_it_is_moved(capsys):
    pages = []
    _move("review", ["type:task"], pages=pages)
    _move("review", ["type:task"], pages=pages)
    assert len(pages) == 1
    assert "already paged" in capsys.readouterr().out


def test_no_other_state_ever_pages_anyone():
    for state in ("refine", "ready", "doing", "ai-completed", "blocked-on-human", "done"):
        pages = []
        _move(state, ["type:task"], pages=pages)
        assert pages == [], state


def test_a_page_that_fails_neither_fails_the_move_nor_burns_the_once_per_issue_marker(capsys):
    current = {"labels": [], "state": "OPEN", "title": "A title", "url": "u"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "label_exists", return_value=False),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "mirror_board_column", return_value="board:    mirrored"),
        patch.object(issues, "page_human", return_value=False),
    ):
        issues.cmd_move(argparse.Namespace(numbers=[7], state="review"))
    # The label was written all the same -- that is the move, and it already happened.
    assert update.call_args[0][2]["labels"] == ["status:review"]
    assert "nobody was paged" in capsys.readouterr().out
    assert not (issues.REVIEW_PAGES_DIR / "7").exists()


def test_a_template_move_cannot_render_is_reported_and_leaves_the_move_standing(capsys):
    """A literal `{` in a Spanish sentence used to raise out of `cmd_move` -- `str.format` throws
    ValueError there, not KeyError -- after the label and the board column were already written
    (#366 review). The config validator refuses such a template on load; this is the second net."""
    current = {"labels": [], "state": "OPEN", "title": "A title", "url": "u"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "label_exists", return_value=False),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "mirror_board_column", return_value="board:    mirrored"),
        patch.object(
            issues,
            "render_human_message",
            side_effect=HumanMessageError("review_ready is not a valid template"),
        ),
        patch.object(issues, "page_human") as page,
    ):
        issues.cmd_move(argparse.Namespace(numbers=[7], state="review"))
    assert update.call_args[0][2]["labels"] == ["status:review"]
    page.assert_not_called()
    assert "not sent" in capsys.readouterr().out
    assert not (issues.REVIEW_PAGES_DIR / "7").exists()


def test_the_once_per_issue_marker_is_claimed_atomically_not_checked_then_written(tmp_path):
    """Two `move N review` racing each other both passed an `exists()` check and both paged. The
    claim is an exclusive create now, so the second one loses on the file system, not on timing:
    this proves it by creating the marker first and watching the page never go out."""
    issue = {"title": "A title", "url": "u"}
    (issues.REVIEW_PAGES_DIR).mkdir(parents=True, exist_ok=True)
    (issues.REVIEW_PAGES_DIR / "7").write_text("claimed by the other move\n")
    with patch.object(issues, "page_human") as page:
        line = issues.page_review_ready(7, issue)
    page.assert_not_called()
    assert "already paged" in line
    # And the claim is written BEFORE the page, so the rival that loses the race sees it there.
    (issues.REVIEW_PAGES_DIR / "7").unlink()
    seen_at_page_time = []
    with patch.object(
        issues,
        "page_human",
        side_effect=lambda message: (
            seen_at_page_time.append((issues.REVIEW_PAGES_DIR / "7").exists()) is None
        ),
    ):
        issues.page_review_ready(7, issue)
    assert seen_at_page_time == [True]


def test_a_failed_page_releases_the_claim_so_the_next_move_may_still_try():
    issue = {"title": "A title", "url": "u"}
    with patch.object(issues, "page_human", return_value=False):
        assert "nobody was paged" in issues.page_review_ready(7, issue)
    assert not (issues.REVIEW_PAGES_DIR / "7").exists()
    with patch.object(issues, "page_human", return_value=True) as page:
        issues.page_review_ready(7, issue)
    page.assert_called_once()


def test_page_human_survives_a_notify_script_that_is_not_there(monkeypatch):
    monkeypatch.setattr(
        issues.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gone"))
    )
    assert issues.page_human("anything") is False


def test_mirror_board_column_says_so_instead_of_failing_when_the_issue_is_not_on_the_board():
    with patch.object(issues, "board_item_id", return_value=None):
        line = issues.mirror_board_column("owner/name", 7, "Review", 1)
    assert "not on project 1" in line


def test_mirror_board_column_edits_the_status_option_of_that_item():
    with (
        patch.object(issues, "board_item_id", return_value="ITEM"),
        patch.object(issues, "board_status_field", return_value=("PVT", "F2", {"Review": "O1"})),
        patch.object(issues, "gh_json") as edit,
    ):
        line = issues.mirror_board_column("owner/name", 7, "Review", 1)
    args = edit.call_args[0]
    assert args[:2] == ("project", "item-edit")
    assert args[args.index("--id") + 1] == "ITEM"
    assert args[args.index("--project-id") + 1] == "PVT"
    assert args[args.index("--field-id") + 1] == "F2"
    assert args[args.index("--single-select-option-id") + 1] == "O1"
    assert line == "board:    Review"


def _project_items(*nodes: tuple[str, int, str]) -> dict:
    """What `gh api graphql` answers for an issue's `projectItems`: (item id, board, owner)."""
    return {
        "data": {
            "repository": {
                "issue": {
                    "projectItems": {
                        "nodes": [
                            {"id": item, "project": {"number": board, "owner": {"login": login}}}
                            for item, board, login in nodes
                        ]
                    }
                }
            }
        }
    }


def _board_gh(issue_side: dict, listing: dict | None = None):
    """A `gh_json` that answers the issue-side query and, if anything asks, the board listing --
    empty by default, which is what an org Project v2 returned in #14 for items it did hold."""

    def fake(*args, **_kwargs):
        if args[:2] == ("api", "graphql"):
            return issue_side
        if args[:2] == ("project", "item-list"):
            return listing if listing is not None else {"items": [], "totalCount": 0}
        raise AssertionError(f"unexpected gh call: {args}")

    return fake


def test_board_item_id_finds_the_item_the_board_listing_leaves_out():
    """#14: `gh project item-list` on an org Project v2 came back with zero items while every
    issue's `projectItems` named its item on that board, so every `move` skipped the mirror."""
    fake = _board_gh(_project_items(("PVTI_mine", 2, "owner")))
    with patch.object(issues, "gh_json", side_effect=fake):
        assert issues.board_item_id("owner", 2, "owner/name", 37) == "PVTI_mine"


def test_board_item_id_picks_the_item_on_this_board_of_this_owner():
    fake = _board_gh(
        _project_items(
            ("OTHER_BOARD", 3, "owner"), ("OTHER_OWNER", 2, "else"), ("MINE", 2, "owner")
        )
    )
    with patch.object(issues, "gh_json", side_effect=fake):
        assert issues.board_item_id("owner", 2, "owner/name", 7) == "MINE"


def test_board_item_id_is_none_when_the_issue_is_on_no_board():
    with patch.object(issues, "gh_json", side_effect=_board_gh(_project_items())):
        assert issues.board_item_id("owner", 2, "owner/name", 7) is None


def test_board_item_id_asks_for_that_issue_of_that_repository():
    seen = []

    def fake(*args, **kwargs):
        seen.append(args)
        return _project_items(("MINE", 2, "owner"))

    with patch.object(issues, "gh_json", side_effect=fake):
        issues.board_item_id("owner", 2, "owner/name", 7)
    (args,) = seen
    assert args[:2] == ("api", "graphql")
    assert "owner=owner" in args and "name=name" in args and "number=7" in args


# ---- what a move costs on the GraphQL quota (#27) --------------------------------------------
# `gh`'s GraphQL quota is 5000 points an hour, per user, shared by every host and every tool the
# human runs. Before #27 one no-op `move` cost ~224 points on a 91-item board: `project
# field-list` and `project item-list` page the whole board, so the price grew with it, and 70
# moves could not fit in an hour. These tests stand a fake `gh` in for the real one and count
# what reaches GitHub's GraphQL API, whatever the board's size.

BOARD_COLUMNS = {"refine": "Backlog", "ready": "Ready for AI", "review": "Review"}


def _board_fields_answer(status_name: str = "Status") -> dict:
    """What the one board query answers: the project's id and its fields, one single-select."""
    return {
        "data": {
            "repositoryOwner": {
                "projectV2": {
                    "id": "PVT_board",
                    "fields": {
                        "nodes": [
                            {"id": "F_title", "name": "Title"},
                            {
                                "id": "F_status",
                                "name": status_name,
                                "options": [
                                    {"id": f"O_{column}", "name": column}
                                    for column in BOARD_COLUMNS.values()
                                ],
                            },
                        ]
                    },
                }
            }
        }
    }


class CountingGh:
    """A stand-in for the `gh` binary, at the `_gh` seam: answers every call `move` makes and
    records it as GraphQL or REST. A call that would page the whole board (`item-list`,
    `field-list`, `project view`) is refused outright, so a lookup whose cost grows with the board
    can never pass by accident."""

    BOARD_PAGING = frozenset(
        {("project", "item-list"), ("project", "field-list"), ("project", "view")}
    )

    def __init__(self):
        self.missing_labels: set[str] = set()
        self.failing_patches: set[int] = set()
        self.graphql: list[tuple] = []
        self.rest: list[tuple] = []

    @staticmethod
    def is_graphql(args: tuple) -> bool:
        if args[0] == "api":
            return args[1] == "graphql"
        # `label create` is REST; every other `gh <noun> <verb>` is GraphQL underneath.
        return args[:2] != ("label", "create")

    def __call__(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        if args[:2] in self.BOARD_PAGING:
            raise AssertionError(f"pages the whole board: {args}")
        (self.graphql if self.is_graphql(args) else self.rest).append(args)
        if args[:2] == ("api", "graphql"):
            query = next(arg for arg in args if arg.startswith("query="))
            if "projectItems" in query:
                number = int(next(arg for arg in args if arg.startswith("number=")).split("=")[1])
                return self.json(_project_items((f"PVTI_{number}", 5, "owner")))
            if "projectV2(" in query:
                return self.json(_board_fields_answer())
            raise AssertionError(f"unexpected graphql query: {query}")
        if args[:2] == ("project", "item-edit"):
            return self.json({"id": "edited"})
        if args[:2] == ("label", "create"):
            return _completed()
        if args[0] == "api" and "/labels/" in args[1]:
            name = urllib.parse.unquote(args[1].rsplit("/", 1)[1])
            if name in self.missing_labels:
                return _completed(returncode=1, stderr="gh: Not Found (HTTP 404)")
            return self.json({"name": name})
        if args[0] == "api" and "/issues/" in args[1]:
            number = int(args[1].rsplit("/", 1)[1])
            if "PATCH" in args:
                if number in self.failing_patches:
                    return _completed(returncode=1, stderr="gh: Gone (HTTP 410)")
                return self.json({"number": number})
            return self.json(
                {
                    "number": number,
                    "labels": [{"name": "type:task"}, {"name": "status:ready"}],
                    "state": "open",
                    "title": f"Issue {number}",
                    "html_url": f"https://github.invalid/owner/name/issues/{number}",
                    "url": f"https://api.github.invalid/repos/owner/name/issues/{number}",
                }
            )
        raise AssertionError(f"unexpected gh call: {args}")

    @staticmethod
    def json(data) -> subprocess.CompletedProcess:
        return _completed(stdout=json.dumps(data))


@pytest.fixture
def counting_gh(monkeypatch):
    """A fresh `CountingGh` wired in for `gh`, with a board configured and no real sleeps."""
    fake = CountingGh()
    monkeypatch.setattr(issues, "_gh", fake)
    monkeypatch.setattr(issues.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(issues, "repo_name", lambda: "owner/name")
    monkeypatch.setattr(
        issues, "load_project", lambda: _project(board_number=5, board_columns=BOARD_COLUMNS)
    )
    return fake


def _move_many(numbers, state):
    issues.cmd_move(argparse.Namespace(numbers=list(numbers), state=state))


def test_one_move_costs_three_graphql_requests_whatever_the_boards_size(counting_gh, capsys):
    """The board's field, the issue's item, the edit: nothing else touches GraphQL. The issue and
    its labels are read and written over REST, which draws on the separate core quota."""
    _move_many([7], "refine")
    kinds = [args[:2] for args in counting_gh.graphql]
    assert kinds == [("api", "graphql"), ("api", "graphql"), ("project", "item-edit")]
    assert "board:    Backlog" in capsys.readouterr().out


def test_move_reads_the_issue_over_rest_not_with_gh_issue_view(counting_gh):
    _move_many([7], "refine")
    assert ("api", "repos/owner/name/issues/7") in [args[:2] for args in counting_gh.rest]
    assert not [args for args in counting_gh.graphql if args[:2] == ("issue", "view")]


def test_move_checks_its_one_label_over_rest_not_by_listing_every_label(counting_gh):
    _move_many([7], "refine")
    assert not [args for args in counting_gh.graphql if args[:2] == ("label", "list")]
    assert ("api", "repos/owner/name/labels/status%3Arefine") in [
        args[:2] for args in counting_gh.rest
    ]
    assert not [args for args in counting_gh.rest if args[:2] == ("label", "create")]


def test_move_creates_its_label_only_when_github_says_it_is_not_there(counting_gh):
    counting_gh.missing_labels.add("status:refine")
    _move_many([7], "refine")
    (create,) = [args for args in counting_gh.rest if args[:2] == ("label", "create")]
    assert create[2] == "status:refine"


def test_a_bulk_move_resolves_the_board_field_and_the_label_once(counting_gh, capsys):
    """N issues in one invocation: two GraphQL requests each (the item, the edit) plus one for the
    board's field, shared by all of them -- never the board's size, never N field lookups."""
    numbers = list(range(1, 21))
    _move_many(numbers, "refine")
    field_lookups = [args for args in counting_gh.graphql if "projectV2(" in " ".join(args)]
    assert len(field_lookups) == 1
    assert len(counting_gh.graphql) == 1 + 2 * len(numbers)
    label_checks = [args for args in counting_gh.rest if "/labels/" in args[1]]
    assert len(label_checks) == 1
    patched = [args[1] for args in counting_gh.rest if "PATCH" in args]
    assert patched == [f"repos/owner/name/issues/{number}" for number in numbers]
    out = capsys.readouterr().out
    assert out.count("board:    Backlog") == len(numbers)
    assert "#1\n" in out and "#20\n" in out


def test_a_bulk_move_goes_on_past_an_issue_that_fails_and_exits_non_zero(counting_gh, capsys):
    counting_gh.failing_patches.add(2)
    with pytest.raises(SystemExit) as exit_info:
        _move_many([1, 2, 3], "refine")
    assert "#2" in str(exit_info.value.code)
    assert "#1" not in str(exit_info.value.code)
    patched = [args[1] for args in counting_gh.rest if "PATCH" in args]
    assert patched[-1] == "repos/owner/name/issues/3"
    assert "HTTP 410" in capsys.readouterr().out


def test_a_single_move_that_fails_still_exits_with_what_gh_said(counting_gh):
    counting_gh.failing_patches.add(7)
    with pytest.raises(SystemExit, match="HTTP 410"):
        _move_many([7], "refine")


def test_the_board_field_is_asked_for_in_one_bounded_query(counting_gh):
    fields = issues.board_status_field("owner", 5)
    assert fields == (
        "PVT_board",
        "F_status",
        {column: f"O_{column}" for column in BOARD_COLUMNS.values()},
    )
    (query,) = counting_gh.graphql
    assert "number=5" in query and "owner=owner" in query


def test_the_board_field_is_asked_for_once_per_process(counting_gh):
    issues.board_status_field("owner", 5)
    issues.board_status_field("owner", 5)
    assert len(counting_gh.graphql) == 1


def test_a_board_whose_column_field_has_another_name_falls_back_to_its_first_single_select(
    monkeypatch,
):
    monkeypatch.setattr(issues, "gh_json", lambda *args, **kwargs: _board_fields_answer("Stage"))
    assert issues.board_status_field("owner", 5)[1] == "F_status"


def test_a_board_with_no_single_select_field_exits_with_a_reason(monkeypatch):
    answer = _board_fields_answer()
    answer["data"]["repositoryOwner"]["projectV2"]["fields"]["nodes"] = [{"id": "F", "name": "T"}]
    monkeypatch.setattr(issues, "gh_json", lambda *args, **kwargs: answer)
    with pytest.raises(SystemExit, match="no single-select field"):
        issues.board_status_field("owner", 5)


def test_a_board_that_does_not_exist_exits_with_a_reason(monkeypatch):
    missing = {"data": {"repositoryOwner": {"projectV2": None}}}
    monkeypatch.setattr(issues, "gh_json", lambda *args, **kwargs: missing)
    with pytest.raises(SystemExit, match="no project owner/5"):
        issues.board_status_field("owner", 5)


def test_the_full_label_listing_create_and_load_use_is_rest_too(monkeypatch):
    """`ensure_fixed_labels` (create, update, load) lists every label once per invocation: over
    REST, every page, not `gh label list`, which is GraphQL."""
    seen = []

    def gh(*args, input_text=None):
        seen.append(args)
        return _completed(stdout="type:task\nstatus:doing\n")

    monkeypatch.setattr(issues, "_gh", gh)
    assert issues.existing_labels("owner/name") == {"type:task", "status:doing"}
    ((command, path, *rest),) = seen
    assert (command, path) == ("api", "repos/owner/name/labels?per_page=100")
    assert "--paginate" in rest


def test_a_label_check_that_fails_for_another_reason_than_404_exits(monkeypatch):
    monkeypatch.setattr(
        issues, "_gh", lambda *args, **kwargs: _completed(returncode=1, stderr="HTTP 502")
    )
    with pytest.raises(SystemExit, match="HTTP 502"):
        issues.label_exists("owner/name", "status:refine")


def test_the_cli_still_takes_one_number_and_now_also_takes_several(monkeypatch):
    seen = []
    monkeypatch.setattr(issues, "cmd_move", seen.append)
    for argv in (["move", "7", "review"], ["move", "2", "3", "4", "refine"]):
        monkeypatch.setattr(issues.sys, "argv", ["issues", *argv])
        issues.main()
    assert [(args.numbers, args.state) for args in seen] == [
        ([7], "review"),
        ([2, 3, 4], "refine"),
    ]


# ---- the brief a worker starts from ----------------------------------------------------------


ISSUE = {"number": 347, "title": "The unit of work", "body": "## Objective\nDo it.", "url": "u/347"}
PARENT = {"number": 336, "title": "The plan", "body": "Why it exists.", "url": "u/336"}


def test_compose_brief_lays_out_issue_then_parent_then_supplement():
    text = issues.compose_brief(ISSUE, PARENT, "Read this too.\n")
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Issue #347 — The unit of work",
        "## Objective",
        "# Parent issue #336 — The plan",
        "## Supplement",
    ]
    assert text.index("Do it.") < text.index("Why it exists.") < text.index("Read this too.")
    assert "u/347" in text and "u/336" in text


def test_compose_brief_says_there_is_no_parent_rather_than_dropping_the_section():
    # A missing section reads as "the assembly failed" exactly like "there is nothing above this".
    text = issues.compose_brief(ISSUE, None, None)
    assert "# Parent issue" in text
    assert "no parent" in text
    assert "## Supplement" not in text


def test_fetch_brief_sources_reads_the_parent_named_by_the_issue():
    with patch.object(
        issues, "gh_json_dict", side_effect=[dict(ISSUE, parent={"number": 336}), PARENT]
    ) as view:
        issue, parent = issues.fetch_brief_sources("owner/name", 347)
    assert parent["number"] == 336
    assert issue["number"] == 347
    assert [call[0][2] for call in view.call_args_list] == ["347", "336"]


def test_fetch_brief_sources_makes_one_call_for_an_issue_with_no_parent():
    with patch.object(issues, "gh_json_dict", return_value=dict(ISSUE, parent=None)) as view:
        _, parent = issues.fetch_brief_sources("owner/name", 347)
    assert parent is None
    assert view.call_count == 1


def test_cmd_brief_writes_the_assembled_file(tmp_path):
    supplement = tmp_path / "extra.md"
    supplement.write_text("Extra context.")
    output = tmp_path / "brief.md"
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "fetch_brief_sources", return_value=(ISSUE, PARENT)),
    ):
        issues.cmd_brief(
            argparse.Namespace(number=347, supplement=str(supplement), output=str(output))
        )
    text = output.read_text()
    assert "## Supplement" in text and "Extra context." in text


# ---- supersede: the refiner's split, closed out deterministically (#39) ----------------------


def test_replace_blocker_rewrites_the_original_line_to_every_child_in_its_indentation():
    body = "## Dependencies\n  Blocked by #15\nBlocked by #3\n\n## Definition of done\nx\n"
    assert replace_blocker(body, 15, [84, 85]) == (
        "## Dependencies\n  Blocked by #84\n  Blocked by #85\nBlocked by #3\n\n"
        "## Definition of done\nx\n"
    )


def test_replace_blocker_never_duplicates_a_child_the_body_already_lists():
    body = "## Dependencies\nBlocked by #84\nBlocked by #15"
    assert replace_blocker(body, 15, [84, 85]) == "## Dependencies\nBlocked by #84\nBlocked by #85"


def test_replace_blocker_leaves_a_body_that_does_not_name_the_original_alone():
    assert replace_blocker("## Dependencies\nBlocked by #150\nsee #15", 15, [84]) is None


def _supersede(rows, *, original_state="OPEN", original_labels=(), routes=None, children=(84, 85)):
    """`supersede` against a fake tracker: `gh issue view` of the original, `gh issue list` of the
    open issues, every write recorded. Never shells out."""
    view = {"state": original_state, "labels": [{"name": n} for n in original_labels]}
    calls = []

    def fake_gh_json(*args, **_kwargs):
        calls.append(("gh", args))
        return rows if args[:2] == ("issue", "list") else {}

    def fake_update(repo, number, fields):
        calls.append(("update", number, fields))
        return {}

    with (
        patch.object(issues, "gh_json_dict", return_value=view),
        patch.object(issues, "gh_json", side_effect=fake_gh_json),
        patch.object(issues, "update_issue", side_effect=fake_update),
    ):
        lines = issues.supersede("owner/name", 15, list(children), routes or {})
    return lines, calls


SPLIT_ROWS = [
    {"number": 15, "body": "the original"},
    {"number": 84, "body": "child a"},
    {"number": 85, "body": "child b"},
    {"number": 20, "body": "## Dependencies\nBlocked by #15\n"},
    {"number": 21, "body": "## Dependencies\nBlocked by #15\nBlocked by #3\n"},
    {"number": 22, "body": "## Dependencies\nnone\n"},
]


def test_supersede_repoints_every_dependent_comments_on_it_and_closes_the_original():
    lines, calls = _supersede(SPLIT_ROWS, routes={21: [85]})
    updates = {call[1]: call[2] for call in calls if call[0] == "update"}
    assert updates[20] == {"body": "## Dependencies\nBlocked by #84\nBlocked by #85\n"}
    assert updates[21] == {"body": "## Dependencies\nBlocked by #85\nBlocked by #3\n"}
    assert updates[15] == {"state": "closed", "state_reason": "not_planned"}
    assert 22 not in updates
    comments = {
        call[1][1]: call[1][-1] for call in calls if call[0] == "gh" and call[1][0] == "api"
    }
    assert "#15 was split into #84, #85" in comments["repos/owner/name/issues/20/comments"]
    assert comments["repos/owner/name/issues/15/comments"] == "body=Superseded by #84, #85."
    # The dependents are repointed before the original closes: a run cut in between leaves every
    # dependent still blocked, never unblocked early.
    order = [call[1] for call in calls if call[0] == "update"]
    assert order[-1] == 15
    assert lines[-1] == "closed #15 as not planned, superseded by #84, #85"


def test_supersede_run_twice_changes_nothing_the_second_time():
    rewritten = [dict(row) for row in SPLIT_ROWS if row["number"] != 15]
    for row in rewritten:
        row["body"] = row["body"].replace("#15", "#84")
    lines, calls = _supersede(rewritten, original_state="CLOSED")
    assert [call for call in calls if call[0] == "update"] == []
    assert lines == ["#15 was already closed"]


def test_supersede_refuses_a_child_that_is_not_an_open_issue():
    with pytest.raises(SystemExit, match="#99 is not an open issue"):
        _supersede(SPLIT_ROWS, children=(84, 99))


def test_supersede_refuses_a_feature_whose_children_are_its_parts():
    feature = issues.type_labels()["feature"]
    with pytest.raises(SystemExit, match="only a split task or bug"):
        _supersede(SPLIT_ROWS, original_labels=(feature,))


def test_parse_routes_refuses_a_child_outside_the_split():
    with pytest.raises(SystemExit, match="#7 is not one of the --by children"):
        issues.parse_routes(["21=7"], [84, 85])
    assert issues.parse_routes(["#21=#85, 84"], [84, 85]) == {21: [85, 84]}
