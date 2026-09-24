"""`agent_os.install` -- `agent-os install [--dry-run] [--force]` writes the systemd units
(agent_os/docs/AGENT_OS.md §7 row (h), issue #511). Copying the host's templates
(`.claude/agents/*.md`, the issue templates, the CI snippet) is covered separately by
`test_install_templates.py`.

Pure filesystem and subprocess (`git`, never `gh`, never a real backend). This file must not
request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agent_os.cli import AGENT_OS_DIR, host_root
from agent_os.install import (
    Action,
    InstallError,
    executables_path_prefix,
    plan_systemd_units,
    render_systemd_unit,
    resolve_exec_start,
)
from agent_os.lib import ProjectConfig

# The HOST project this suite runs inside: not a fixed nesting under AGENT_OS_DIR (that
# breaks the moment a copy IS the mechanism's own top directory, as the out-of-tree proof
# makes it -- #512), but whatever `host_root()` itself resolves: the git checkout's toplevel,
# same as every real driver run.
ROOT = host_root()


def _project(**overrides) -> ProjectConfig:
    base = {"repo": "owner/name", "tracking_epic": 1, "board_number": 1, "guard_unit": "acme-guard"}
    base.update(overrides)
    return ProjectConfig(**base)


# --------------------------------------------------------------------------------------------
# PATH and ExecStart
# --------------------------------------------------------------------------------------------


def test_executables_path_prefix_is_just_the_standard_tail_when_none_configured():
    from agent_os.install import DEFAULT_PATH_TAIL

    assert executables_path_prefix(_project()) == DEFAULT_PATH_TAIL


def test_executables_path_prefix_prepends_configured_dirs_without_repeats():
    project = _project(executables={"qwen": "/opt/nvm/bin/qwen", "claude": "/opt/nvm/bin/claude"})
    prefix = executables_path_prefix(project)
    assert prefix.startswith("/opt/nvm/bin:"), prefix
    assert prefix.count("/opt/nvm/bin") == 1, "the same directory twice must collapse to one"


def test_resolve_exec_start_uses_the_hosts_shim_when_present(tmp_path):
    (tmp_path / "scripts").mkdir()
    shim = tmp_path / "scripts" / "agent_guard.py"
    shim.write_text("# shim\n")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.write_text("#!/bin/sh\n")

    exec_start = resolve_exec_start(tmp_path)
    assert exec_start == f"{venv_python} {shim} tick"


def test_resolve_exec_start_falls_back_to_the_module_form_without_a_shim(tmp_path):
    exec_start = resolve_exec_start(tmp_path)
    assert exec_start.endswith("-m agent_os.guard tick")
    assert "scripts/agent_guard.py" not in exec_start


# --------------------------------------------------------------------------------------------
# Rendering the systemd templates
# --------------------------------------------------------------------------------------------


def test_render_systemd_unit_refuses_without_a_configured_guard_unit(tmp_path):
    with pytest.raises(InstallError, match="guard_unit"):
        render_systemd_unit("guard.service", _project(guard_unit=""), tmp_path)


def test_render_guard_service_names_the_host_root_and_the_guard_unit(tmp_path):
    rendered = render_systemd_unit("guard.service", _project(), tmp_path)
    assert f"WorkingDirectory={tmp_path}" in rendered
    assert "Type=oneshot" in rendered
    assert "acme-guard" in rendered
    assert "__" not in rendered, "no token should survive rendering"


def test_render_guard_timer_carries_the_schedule_and_no_leftover_token(tmp_path):
    rendered = render_systemd_unit("guard.timer", _project(), tmp_path)
    assert "OnUnitActiveSec=5min" in rendered
    assert "acme-guard" in rendered
    assert "__" not in rendered


def test_render_override_conf_sets_killmode_process_and_the_path(tmp_path):
    rendered = render_systemd_unit(
        "override.conf", _project(executables={"claude": "/opt/bin/claude"}), tmp_path
    )
    assert "KillMode=process" in rendered
    assert "/opt/bin:" in rendered
    assert "__" not in rendered


# --------------------------------------------------------------------------------------------
# `Action`: what would happen to one file
# --------------------------------------------------------------------------------------------


def test_action_reports_would_create_for_an_absent_file(tmp_path):
    action = Action(tmp_path / "new.txt", "content\n")
    assert action.status(force=False) == "would create"
    assert action.should_write(force=False)


def test_action_reports_up_to_date_and_skips_when_content_matches(tmp_path):
    path = tmp_path / "same.txt"
    path.write_text("content\n")
    action = Action(path, "content\n")
    assert "up to date" in action.status(force=False)
    assert not action.should_write(force=False)


def test_action_refuses_to_overwrite_a_differing_file_without_force(tmp_path):
    path = tmp_path / "differs.txt"
    path.write_text("old\n")
    action = Action(path, "new\n")
    assert "refusing without --force" in action.status(force=False)
    assert not action.should_write(force=False)
    assert "-old" in action.diff and "+new" in action.diff


def test_action_overwrites_a_differing_file_with_force(tmp_path):
    path = tmp_path / "differs.txt"
    path.write_text("old\n")
    action = Action(path, "new\n")
    assert "overwriting (--force)" in action.status(force=True)
    assert action.should_write(force=True)
    action.write()
    assert path.read_text() == "new\n"


# --------------------------------------------------------------------------------------------
# The three plans `install` assembles
# --------------------------------------------------------------------------------------------


def test_plan_systemd_units_returns_service_timer_and_override(tmp_path):
    actions = plan_systemd_units(_project(), tmp_path, tmp_path / "systemd_user")
    destinations = {action.dest.name for action in actions}
    assert destinations == {"acme-guard.service", "acme-guard.timer", "override.conf"}


# --------------------------------------------------------------------------------------------
# `main()` end to end, over `AGENTS_CONFIG_PATH`/`AGENT_OS_HOST_ROOT`, in a subprocess so it never
# touches this machine's real `~/.config/systemd/user/`.
# --------------------------------------------------------------------------------------------


def _run_install(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "agent_os.install", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _isolated_environment(tmp_path, *, guard_unit="acme-guard"):
    host_root = tmp_path / "host"
    host_root.mkdir()
    (host_root / "scripts").mkdir()
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        "project:\n"
        "  repo: owner/name\n"
        "  tracking_epic: 1\n"
        "  board_number: 1\n"
        f"  guard_unit: {guard_unit}\n"
        "classes: {}\n"
    )
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    environment = dict(os.environ)
    environment.update(
        AGENT_OS_HOST_ROOT=str(host_root),
        AGENTS_CONFIG_PATH=str(config_path),
        HOME=str(fake_home),
        PYTHONPATH=str(AGENT_OS_DIR),
    )
    return environment, host_root, fake_home


def test_main_dry_run_creates_nothing(tmp_path):
    environment, _host_root, fake_home = _isolated_environment(tmp_path)

    result = _run_install(environment, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would create" in result.stdout
    systemd_dir = fake_home / ".config" / "systemd" / "user"
    assert not systemd_dir.exists(), "a dry run must write nothing"


def test_main_without_dry_run_writes_the_units(tmp_path):
    environment, _host_root, fake_home = _isolated_environment(tmp_path)

    result = _run_install(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    systemd_dir = fake_home / ".config" / "systemd" / "user"
    assert (systemd_dir / "acme-guard.service").is_file()
    assert (systemd_dir / "acme-guard.timer").is_file()
    assert (systemd_dir / "acme-guard.service.d" / "override.conf").is_file()


def test_main_refuses_to_overwrite_a_differing_file_without_force(tmp_path):
    environment, _host_root, fake_home = _isolated_environment(tmp_path)
    first = _run_install(environment)
    assert first.returncode == 0, first.stdout + first.stderr

    # Change the guard unit name so the second run's rendered content differs from what is on disk
    # under the FIRST run's name -- rerunning with a config that renders identically is a no-op,
    # which is not what this test is about.
    systemd_dir = fake_home / ".config" / "systemd" / "user"
    (systemd_dir / "acme-guard.service").write_text("hand-edited\n")

    second = _run_install(environment)
    assert second.returncode != 0
    assert "refusing without --force" in second.stdout
    assert (systemd_dir / "acme-guard.service").read_text() == "hand-edited\n"


def test_main_force_overwrites_a_differing_file(tmp_path):
    environment, _host_root, fake_home = _isolated_environment(tmp_path)
    first = _run_install(environment)
    assert first.returncode == 0, first.stdout + first.stderr

    systemd_dir = fake_home / ".config" / "systemd" / "user"
    (systemd_dir / "acme-guard.service").write_text("hand-edited\n")

    result = _run_install(environment, "--force")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hand-edited" not in (systemd_dir / "acme-guard.service").read_text()


def test_main_refuses_without_a_configured_guard_unit(tmp_path):
    environment, _host_root, _fake_home = _isolated_environment(tmp_path, guard_unit="")
    result = _run_install(environment, "--dry-run")
    assert result.returncode != 0
    assert "guard_unit" in (result.stdout + result.stderr)
