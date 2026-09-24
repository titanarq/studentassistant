"""No-network tests for `agent_os.issues`: the backlog YAML -> GitHub Issue mapping, the
fixed labels and the repository, both read from `config/agents.yaml`, the key-line parser, and the
`gh` subprocess plumbing (mocked — never actually shells out)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
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


def test_gh_json_retries_on_rate_limit_then_succeeds():
    responses = [
        _completed(returncode=1, stderr="HTTP 403: API rate limit exceeded"),
        _completed(returncode=0, stdout='{"ok": true}'),
    ]
    with (
        patch("agent_os.issues.subprocess.run", side_effect=responses) as run,
        patch("agent_os.issues.time.sleep") as sleep,
    ):
        assert issues.gh_json("issue", "list") == {"ok": True}
        assert run.call_count == 2
        sleep.assert_called_once()


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


def test_create_from_a_template_sends_the_scaffold_as_the_body_and_infers_the_type(
    shipped_issue_templates,
):
    created = {"number": 9, "id": 99, "html_url": "u"}
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "ensure_fixed_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "create_issue", return_value=created) as create,
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
    """`validate_issue` with `gh issue view` mocked to return `body` and `gh issue list` to return
    `open_numbers`. Never shells out."""
    listing = [{"number": n} for n in open_numbers]
    with (
        patch.object(issues, "gh_json_dict", return_value={"body": body}),
        patch.object(issues, "gh_json", return_value=listing),
    ):
        return issues.validate_issue("owner/name", 1)


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
        patch.object(issues, "gh_json") as listing,
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
        patch.object(issues, "gh_json") as listing,
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


def _move(state, held_labels, *, issue_state="OPEN", pages=None, title="A title"):
    """Runs `cmd_move` with every `gh` call mocked. Returns `(fields, board_line)`: what would
    have been PATCHed onto the issue, and what the board mirror printed. `pages` collects whatever
    reached `agent_os/bin/notify.sh`; the marker directory is redirected per call so "once per issue"
    is exercised deliberately, never inherited from a previous test."""
    current = {
        "labels": [{"name": name} for name in held_labels],
        "state": issue_state,
        "title": title,
        "url": "https://github.invalid/owner/name/issues/7",
    }
    sent = pages if pages is not None else []
    with (
        patch.object(issues, "repo_name", return_value="owner/name"),
        patch.object(issues, "gh_json_dict", return_value=current),
        patch.object(issues, "existing_labels", return_value=set()),
        patch.object(issues, "ensure_labels") as ensure,
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "mirror_board_column", return_value="board:    mirrored") as board,
        patch.object(
            issues, "page_human", side_effect=lambda message: sent.append(message) is None
        ),
    ):
        issues.cmd_move(argparse.Namespace(number=7, state=state))
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
        patch.object(issues, "existing_labels", return_value=set()),
        patch.object(issues, "ensure_labels"),
        patch.object(issues, "update_issue") as update,
        patch.object(issues, "mirror_board_column", return_value="board:    mirrored"),
        patch.object(issues, "page_human", return_value=False),
    ):
        issues.cmd_move(argparse.Namespace(number=7, state="review"))
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
        patch.object(issues, "existing_labels", return_value=set()),
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
        issues.cmd_move(argparse.Namespace(number=7, state="review"))
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
    fields = {
        "fields": [
            {"id": "F1", "name": "Title", "type": "ProjectV2Field"},
            {"id": "F2", "name": "Status", "options": [{"id": "O1", "name": "Review"}]},
        ]
    }
    with (
        patch.object(issues, "board_item_id", return_value="ITEM"),
        patch.object(issues, "gh_json_dict", side_effect=[{"id": "PVT"}, fields]),
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


def test_board_item_id_matches_the_issue_number_in_this_repository():
    listing = {
        "items": [
            {"id": "OTHER", "content": {"number": 7, "repository": "someone/else"}},
            {"id": "MINE", "content": {"number": 7, "repository": "owner/name"}},
        ]
    }
    with patch.object(issues, "gh_json", return_value=listing):
        assert issues.board_item_id("owner", 1, "owner/name", 7) == "MINE"


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
