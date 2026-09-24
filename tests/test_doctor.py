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
        {"name": project.labels.ai_completed},
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


def test_check_labels_passes_when_all_four_exist():
    project = _project()
    existing = [
        {"name": project.labels.ai_completed},
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


def test_check_board_fails_when_an_option_is_missing():
    project = _project()
    response = _fields_response(["Backlog", "Ready for AI", "In progress"])  # 3 of 6
    with patch("agent_os.issues.subprocess.run", return_value=_completed(stdout=response)):
        check = doctor.check_board(project, "owner/name")
    assert not check.ok
    assert "AI completed" in check.detail


def test_check_board_fails_when_no_status_field_exists():
    import json

    response = json.dumps({"fields": [{"name": "Other", "options": [{"name": "x"}]}]})
    with patch("agent_os.issues.subprocess.run", return_value=_completed(stdout=response)):
        check = doctor.check_board(_project(), "owner/name")
    assert not check.ok
    assert "Status" in check.detail


def test_check_board_passes_with_all_six_columns():
    response = _fields_response(
        ["Backlog", "Ready for AI", "In progress", "AI completed", "Review", "Done"]
    )
    with patch("agent_os.issues.subprocess.run", return_value=_completed(stdout=response)):
        check = doctor.check_board(_project(), "owner/name")
    assert check.ok


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
            return _completed(
                stdout=_fields_response(
                    ["Backlog", "Ready for AI", "In progress", "AI completed", "Review", "Done"]
                )
            )
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
    }
