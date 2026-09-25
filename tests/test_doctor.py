"""`agent_os.doctor` -- the first-run checklist of `agent_os/docs/AGENT_OS.md` §6, read back mechanically
(issue #511). Every check reads state only: `gh`, `systemctl --user is-active` and file existence
are all mocked or filesystem-only here, so this suite calls nothing real and starts nothing.

Pure filesystem plus mocked subprocess. This file must not request the `engine` or `db_sandbox`
fixture, and must never patch anything with a real `gh`/`systemctl` call.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from agent_os import doctor
from agent_os.lib import ProjectConfig


def _project(**overrides) -> ProjectConfig:
    fields = {
        "repo": "owner/name",
        "tracking_epic": 1,
        "board_number": 1,
        "guard_unit": "acme-guard",
        "secrets_dir": ".secrets/gh_apps",
        "planner_app": "acme-planner",
        "worker_apps": {"qwen": "acme-qwen", "claude": "acme-claude"},
        "notify_topic_file": ".secrets/ntfy_topic",
        # Relative to `root` with no `../`: `check_worktrees` just joins them, and these tests
        # keep everything under one `tmp_path` rather than mimicking the real "sibling directory"
        # layout `project.worktrees` uses in production.
        "worktrees": {"qwen": "acme-qwen", "claude": "acme-claude"},
        "board_columns": {
            "refine": "Backlog",
            "ready": "Ready for AI",
            "doing": "In progress",
            "blocked-on-human": None,
            "ai-completed": "AI completed",
            "review": "Review",
            "done": "Done",
        },
    }
    fields.update(overrides)
    return ProjectConfig(**fields)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --------------------------------------------------------------------------------------------
# python3 >= 3.12
# --------------------------------------------------------------------------------------------


def test_check_python_version_passes_on_the_interpreter_running_the_suite():
    # The suite itself only runs on >=3.12 (agent_os/pyproject.toml's requires-python), so this is
    # never a false negative by construction.
    check = doctor.check_python_version()
    assert check.ok


# --------------------------------------------------------------------------------------------
# gh auth status
# --------------------------------------------------------------------------------------------


def test_check_gh_auth_fails_when_not_logged_in():
    with patch(
        "agent_os.doctor.subprocess.run",
        return_value=_completed(returncode=1, stderr="not logged in"),
    ):
        check = doctor.check_gh_auth()
    assert not check.ok
    assert "not logged in" in check.detail


def test_check_gh_auth_fails_on_a_missing_scope():
    output = "  - Token scopes: 'gist', 'repo', 'workflow'"
    with patch("agent_os.doctor.subprocess.run", return_value=_completed(stdout=output)):
        check = doctor.check_gh_auth()
    assert not check.ok
    assert "project" in check.detail


def test_check_gh_auth_passes_with_both_required_scopes():
    output = "  - Token scopes: 'gist', 'project', 'repo', 'workflow'"
    with patch("agent_os.doctor.subprocess.run", return_value=_completed(stdout=output)):
        check = doctor.check_gh_auth()
    assert check.ok


# --------------------------------------------------------------------------------------------
# labels that do not autocreate
# --------------------------------------------------------------------------------------------


def test_check_labels_fails_when_one_is_missing():
    project = _project()
    existing = [
        {"name": project.labels.agents_paused},
        # auto-ready and wake:planner both missing
    ]
    with patch(
        "agent_os.issues.subprocess.run",
        return_value=_completed(stdout=__import__("json").dumps(existing)),
    ):
        check = doctor.check_labels(project, "owner/name")
    assert not check.ok
    assert project.labels.auto_ready in check.detail
    assert project.labels.wake_planner in check.detail


def test_check_labels_passes_without_the_ai_completed_state_label():
    # `status:ai-completed` is a state label: `issues.py move N ai-completed` creates it on first
    # use, like every other state label, so a host that has not yet completed a task lacks it and
    # is healthy (#54). Only the labels `move` never writes are required.
    project = _project()
    existing = [
        {"name": project.labels.agents_paused},
        {"name": project.labels.auto_ready},
        {"name": project.labels.wake_planner},
    ]
    with patch(
        "agent_os.issues.subprocess.run",
        return_value=_completed(stdout=__import__("json").dumps(existing)),
    ):
        check = doctor.check_labels(project, "owner/name")
    assert check.ok


# --------------------------------------------------------------------------------------------
# Project v2 Status field
# --------------------------------------------------------------------------------------------


def _fields_response(options):
    import json

    return json.dumps({"fields": [{"name": "Status", "options": [{"name": o} for o in options]}]})


ALL_SIX = ["Backlog", "Ready for AI", "In progress", "AI completed", "Review", "Done"]


def _linked_response(boards):
    """What `gh api graphql` answers for the repository's linked Projects: `(number, owner)`."""
    import json

    nodes = [{"number": number, "owner": {"login": owner}} for number, owner in boards]
    return json.dumps({"data": {"repository": {"projectsV2": {"nodes": nodes}}}})


def _board_gh(fields, linked=((1, "owner"),)):
    """One `subprocess.run` stand-in answering the board check's two `gh` calls."""

    def dispatch(args, **kwargs):
        if args[1:3] == ["project", "field-list"]:
            return _completed(stdout=fields)
        if args[1:3] == ["api", "graphql"]:
            return _completed(stdout=_linked_response(linked))
        raise AssertionError(f"unexpected call: {args}")

    return dispatch


def test_check_board_fails_when_an_option_is_missing():
    project = _project()
    response = _fields_response(["Backlog", "Ready for AI", "In progress"])  # 3 of 6
    with patch("agent_os.issues.subprocess.run", side_effect=_board_gh(response)):
        check = doctor.check_board(project, "owner/name")
    assert not check.ok
    assert "AI completed" in check.detail


def test_check_board_fails_when_no_status_field_exists():
    import json

    response = json.dumps({"fields": [{"name": "Other", "options": [{"name": "x"}]}]})
    with patch("agent_os.issues.subprocess.run", side_effect=_board_gh(response)):
        check = doctor.check_board(_project(), "owner/name")
    assert not check.ok
    assert "Status" in check.detail


def test_check_board_passes_with_all_six_columns():
    response = _fields_response(ALL_SIX)
    with patch("agent_os.issues.subprocess.run", side_effect=_board_gh(response)):
        check = doctor.check_board(_project(), "owner/name")
    assert check.ok


# agent-os#5: `board_number` copied from the example names SOME Project of the owner -- one that
# may belong to another repository and still carry all six columns. The check also reads which
# Projects are linked to `project.repo`, and fails on one that is not.


def test_check_board_fails_on_a_project_not_linked_to_the_repository():
    response = _fields_response(ALL_SIX)
    gh = _board_gh(response, linked=[(2, "owner")])
    with patch("agent_os.issues.subprocess.run", side_effect=gh):
        check = doctor.check_board(_project(board_number=1), "owner/name")
    assert not check.ok
    assert "not linked to owner/name" in check.detail
    assert "gh project link 1 --owner owner --repo name" in check.detail


def test_check_board_fails_when_the_repository_has_no_linked_project():
    gh = _board_gh(_fields_response(ALL_SIX), linked=[])
    with patch("agent_os.issues.subprocess.run", side_effect=gh):
        check = doctor.check_board(_project(board_number=1), "owner/name")
    assert not check.ok
    assert "not linked to owner/name" in check.detail


def test_check_board_fails_on_a_same_numbered_project_of_another_owner():
    gh = _board_gh(_fields_response(ALL_SIX), linked=[(1, "someone-else")])
    with patch("agent_os.issues.subprocess.run", side_effect=gh):
        check = doctor.check_board(_project(board_number=1), "owner/name")
    assert not check.ok


# --------------------------------------------------------------------------------------------
# GitHub App secrets
# --------------------------------------------------------------------------------------------


def test_check_app_secrets_fails_when_a_pair_is_missing(tmp_path):
    project = _project(worker_apps={"claude": "acme-claude"}, role_apps={})
    secrets_dir = tmp_path / ".secrets" / "gh_apps"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "acme-planner.json").write_text("{}")
    (secrets_dir / "acme-planner.pem").write_text("x")
    # acme-claude's pair is entirely missing.
    check = doctor.check_app_secrets(project, tmp_path)
    assert not check.ok
    assert "acme-claude" in check.detail


def test_check_app_secrets_passes_when_every_pair_exists(tmp_path):
    project = _project(worker_apps={"claude": "acme-claude"}, role_apps={})
    secrets_dir = tmp_path / ".secrets" / "gh_apps"
    secrets_dir.mkdir(parents=True)
    for slug in ("acme-planner", "acme-claude"):
        (secrets_dir / f"{slug}.json").write_text("{}")
        (secrets_dir / f"{slug}.pem").write_text("x")
    check = doctor.check_app_secrets(project, tmp_path)
    assert check.ok


# --------------------------------------------------------------------------------------------
# project.executables resolve
# --------------------------------------------------------------------------------------------


def test_check_executables_passes_when_none_are_configured():
    assert doctor.check_executables(_project(executables={})).ok


def test_check_executables_passes_on_an_existing_absolute_path(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert doctor.check_executables(_project(executables={"claude": str(binary)})).ok


def test_check_executables_fails_on_a_nonexistent_absolute_path(tmp_path):
    check = doctor.check_executables(_project(executables={"claude": str(tmp_path / "nope")}))
    assert not check.ok
    assert "claude" in check.detail


def test_check_executables_resolves_a_bare_name_off_path():
    check = doctor.check_executables(_project(executables={"sh": "sh"}))
    assert check.ok


# --------------------------------------------------------------------------------------------
# worktrees exist
# --------------------------------------------------------------------------------------------


def test_check_worktrees_fails_when_one_has_no_git(tmp_path):
    (tmp_path / "acme-claude").mkdir()
    (tmp_path / "acme-claude" / ".git").mkdir()
    check = doctor.check_worktrees(_project(), tmp_path)
    assert not check.ok
    assert "qwen" in check.detail


def test_check_worktrees_passes_when_every_backend_has_one(tmp_path):
    for name in ("acme-qwen", "acme-claude"):
        (tmp_path / name / ".git").mkdir(parents=True)
    check = doctor.check_worktrees(_project(), tmp_path)
    assert check.ok


# --------------------------------------------------------------------------------------------
# notify topic file
# --------------------------------------------------------------------------------------------


def test_check_notify_topic_fails_when_absent(tmp_path):
    assert not doctor.check_notify_topic(_project(), tmp_path).ok


def test_check_notify_topic_passes_when_present(tmp_path):
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "ntfy_topic").write_text("topic\n")
    assert doctor.check_notify_topic(_project(), tmp_path).ok


# --------------------------------------------------------------------------------------------
# guard timer active
# --------------------------------------------------------------------------------------------


def test_check_guard_timer_fails_without_a_configured_guard_unit():
    assert not doctor.check_guard_timer(_project(guard_unit="")).ok


def test_check_guard_timer_fails_when_inactive():
    with patch("agent_os.doctor.subprocess.run", return_value=_completed(stdout="inactive\n")):
        check = doctor.check_guard_timer(_project())
    assert not check.ok


def test_check_guard_timer_passes_when_active():
    with patch("agent_os.doctor.subprocess.run", return_value=_completed(stdout="active\n")):
        check = doctor.check_guard_timer(_project())
    assert check.ok


# --------------------------------------------------------------------------------------------
# A workflow that reports a check on a host-only PR (agent-os#50)
# --------------------------------------------------------------------------------------------


def _workflow(root, name, text):
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


PATH_FILTERED = "on:\n  pull_request:\n    paths:\n      - agent_os/**\njobs: {}\n"


def test_check_pull_request_ci_fails_with_no_workflow_at_all(tmp_path):
    check = doctor.check_pull_request_ci(tmp_path)
    assert not check.ok
    assert "agent-os-install" in check.detail


def test_check_pull_request_ci_fails_when_every_workflow_is_path_filtered(tmp_path):
    _workflow(tmp_path, "ci-agent-os.yml", PATH_FILTERED)
    _workflow(tmp_path, "docs.yml", "on:\n  pull_request:\n    paths-ignore: ['src/**']\n")
    _workflow(tmp_path, "nightly.yml", "on:\n  schedule:\n    - cron: '0 0 * * *'\n")
    check = doctor.check_pull_request_ci(tmp_path)
    assert not check.ok
    assert "condition 1" in check.detail


def test_check_pull_request_ci_passes_on_an_unfiltered_pull_request_trigger(tmp_path):
    _workflow(tmp_path, "ci-agent-os.yml", PATH_FILTERED)
    _workflow(tmp_path, "ci.yml", "on:\n  pull_request:\n    branches: [main]\njobs: {}\n")
    check = doctor.check_pull_request_ci(tmp_path)
    assert check.ok, check.detail
    assert "ci.yml" in check.detail


def test_check_pull_request_ci_reads_the_string_and_list_trigger_forms(tmp_path):
    _workflow(tmp_path, "a.yml", "on: pull_request\n")
    assert doctor.check_pull_request_ci(tmp_path).ok
    _workflow(tmp_path, "a.yml", "on: [push, pull_request]\n")
    assert doctor.check_pull_request_ci(tmp_path).ok


def test_check_pull_request_ci_fails_rather_than_crashes_on_an_unreadable_workflow(tmp_path):
    _workflow(tmp_path, "broken.yaml", "on: [unclosed\n")
    assert not doctor.check_pull_request_ci(tmp_path).ok


# --------------------------------------------------------------------------------------------
# `run_checks`: a full passing checklist and a full failing one, the two the issue asks for.
# --------------------------------------------------------------------------------------------


def test_run_checks_all_pass(tmp_path):
    project = _project()
    for slug in ("acme-planner", "acme-claude", "acme-qwen"):
        secrets = tmp_path / ".secrets" / "gh_apps"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / f"{slug}.json").write_text("{}")
        (secrets / f"{slug}.pem").write_text("x")
    (tmp_path / ".secrets" / "ntfy_topic").write_text("topic\n")
    for name in ("acme-qwen", "acme-claude"):
        (tmp_path / name / ".git").mkdir(parents=True)
    _workflow(tmp_path, "ci-host.yml", "on:\n  pull_request:\njobs: {}\n")

    def dispatch(args, **kwargs):
        # `agent_os.issues` and `agent_os.doctor` both do a plain `import subprocess`, so they
        # share the SAME module-level `run` attribute -- patching each path separately means the
        # second patch silently wins for both, which is why this is one dispatcher on one patch.
        if args[1:3] == ["label", "list"]:
            labels = [
                {"name": project.labels.ai_completed},
                {"name": project.labels.agents_paused},
                {"name": project.labels.auto_ready},
                {"name": project.labels.wake_planner},
            ]
            return _completed(stdout=__import__("json").dumps(labels))
        if args[1:3] == ["project", "field-list"]:
            return _completed(stdout=_fields_response(ALL_SIX))
        if args[1:3] == ["api", "graphql"]:
            return _completed(stdout=_linked_response([(1, "owner")]))
        if args[:2] == ["gh", "auth"]:
            return _completed(stdout="  - Token scopes: 'repo', 'project'")
        if args[:1] == ["systemctl"]:
            return _completed(stdout="active\n")
        raise AssertionError(f"unexpected call: {args}")

    with patch("subprocess.run", side_effect=lambda args, **kw: dispatch(args, **kw)):
        checks = doctor.run_checks(project, tmp_path, "owner/name")

    assert all(check.ok for check in checks), [c.line() for c in checks if not c.ok]


def test_run_checks_reports_each_failure_without_stopping_at_the_first(tmp_path):
    # Nothing set up under tmp_path: secrets, worktrees and the notify file are all missing, and
    # both gh dispatchers answer with an empty/mismatched checklist -- the point is that EVERY
    # check runs and reports, not just the first one to fail.
    project = _project()

    def dispatch(args, **kwargs):
        if args[1:3] == ["label", "list"]:
            return _completed(stdout="[]")
        if args[1:3] == ["project", "field-list"]:
            return _completed(stdout=_fields_response([]))
        if args[1:3] == ["api", "graphql"]:
            return _completed(stdout=_linked_response([(1, "owner")]))
        if args[:2] == ["gh", "auth"]:
            return _completed(returncode=1, stderr="not logged in")
        if args[:1] == ["systemctl"]:
            return _completed(stdout="inactive\n")
        raise AssertionError(f"unexpected call: {args}")

    with patch("subprocess.run", side_effect=lambda args, **kw: dispatch(args, **kw)):
        checks = doctor.run_checks(project, tmp_path, "owner/name")

    failed_names = {check.name for check in checks if not check.ok}
    assert failed_names == {
        "gh auth status",
        "labels that do not autocreate",
        "Project v2 Status field",
        "GitHub App secrets",
        "worktrees exist",
        "notify topic file",
        "guard timer active",
        "a check on every pull request",
    }


# agent-os#10: the hint a failed timer check prints is read on a host that has only what ships in
# `agent_os/`. It says what to run, and any doc it names is one this repository ships.


def _named_docs_exist(text):
    import re

    from agent_os.cli import AGENT_OS_DIR

    named = re.findall(r"(?:agent_os/)?docs/[\w./-]+\.md", text)
    return [path for path in named if not (AGENT_OS_DIR / path.removeprefix("agent_os/")).is_file()]


def test_check_guard_timer_hint_is_self_sufficient_and_names_only_shipped_docs():
    with patch("agent_os.doctor.subprocess.run", return_value=_completed(stdout="inactive\n")):
        check = doctor.check_guard_timer(_project())
    assert not check.ok
    assert "systemctl --user enable --now acme-guard.timer" in check.detail
    assert "agent-os-install" in check.detail
    assert _named_docs_exist(check.detail) == [], check.detail


# --------------------------------------------------------------------------------------------
# A `gh` failure inside one check (agent-os#4): that check turns into a [FAIL] carrying the error
# and every other check still runs -- `gh_json` answers a failure with `sys.exit`, which used to
# end the whole run after one line.
# --------------------------------------------------------------------------------------------


def test_run_checks_turns_a_gh_failure_into_a_failed_check_and_keeps_going(tmp_path):
    missing_repo = "GraphQL: Could not resolve to a Repository with the name 'owner/name'."

    def dispatch(args, **kwargs):
        if args[:2] == ["gh", "auth"]:
            return _completed(stdout="  - Token scopes: 'repo', 'project'")
        if args[:1] == ["gh"]:
            return _completed(returncode=1, stderr=missing_repo)
        if args[:1] == ["systemctl"]:
            return _completed(stdout="inactive\n")
        raise AssertionError(f"unexpected call: {args}")

    with patch("subprocess.run", side_effect=lambda args, **kw: dispatch(args, **kw)):
        checks = doctor.run_checks(_project(), tmp_path, "owner/name")

    by_name = {check.name: check for check in checks}
    assert len(checks) == 10, [c.line() for c in checks]
    labels = by_name["labels that do not autocreate"]
    assert not labels.ok
    assert "Could not resolve to a Repository" in labels.detail
    assert "\n" not in labels.line()
    assert not by_name["Project v2 Status field"].ok
    assert "guard timer active" in by_name


def test_run_checks_reports_a_missing_binary_as_a_failed_check(tmp_path):
    def missing(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    with patch("subprocess.run", side_effect=missing):
        checks = doctor.run_checks(_project(), tmp_path, "owner/name")

    by_name = {check.name: check for check in checks}
    assert len(checks) == 10, [c.line() for c in checks]
    for name in (
        "gh auth status",
        "labels that do not autocreate",
        "Project v2 Status field",
        "guard timer active",
    ):
        assert not by_name[name].ok
        assert "No such file or directory" in by_name[name].detail


# `main()` on a `config/agents.yaml` that is absent or broken (agent-os#3): a [FAIL] line, not a
# traceback. Run as a subprocess over a fake `gh`/`systemctl` on PATH, so nothing real is called.
# --------------------------------------------------------------------------------------------


def _run_doctor(tmp_path, config_path):
    import os
    import sys

    from agent_os.cli import AGENT_OS_DIR

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text("#!/bin/sh\necho \"  - Token scopes: 'repo', 'project'\"\n")
    (fake_bin / "systemctl").write_text("#!/bin/sh\necho inactive\n")
    for script in fake_bin.iterdir():
        script.chmod(0o755)
    host = tmp_path / "host"
    host.mkdir()
    environment = dict(os.environ)
    environment.update(
        AGENT_OS_HOST_ROOT=str(host),
        AGENTS_CONFIG_PATH=str(config_path),
        AGENT_OS_GH_REPO="owner/name",
        PATH=f"{fake_bin}:{environment.get('PATH', '')}",
        PYTHONPATH=str(AGENT_OS_DIR),
    )
    return subprocess.run(
        [sys.executable, "-m", "agent_os.doctor"],
        cwd=host,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_a_missing_config_as_a_failed_check(tmp_path):
    config_path = tmp_path / "absent.yaml"
    result = _run_doctor(tmp_path, config_path)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    config_lines = [line for line in result.stdout.splitlines() if "config/agents.yaml" in line]
    assert len(config_lines) == 1, result.stdout
    assert config_lines[0].startswith("[FAIL]")
    assert str(config_path) in config_lines[0]
    assert "ADOPTION.md step 8" in config_lines[0]
    # The checks that need no config still run and report.
    assert "[ok  ] python3 >= 3.12" in result.stdout
    assert "[ok  ] gh auth status" in result.stdout


def test_main_reports_an_invalid_config_as_a_failed_check(tmp_path):
    config_path = tmp_path / "agents.yaml"
    config_path.write_text("project:\n  repo: owner/name\n  no_such_key: 1\n")
    result = _run_doctor(tmp_path, config_path)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    assert any(
        line.startswith("[FAIL] config/agents.yaml") and "does not load" in line
        for line in result.stdout.splitlines()
    ), result.stdout
