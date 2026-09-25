"""`agent_os.install` -- `agent-os install [--dry-run] [--force]` writes the systemd units
(agent_os/docs/AGENT_OS.md §7 row (h), issue #511). Copying the host's templates
(`.claude/agents/*.md`, the issue templates, the CI snippet) is covered separately by
`test_install_templates.py`.

Pure filesystem and subprocess (`git`, never `gh`, never a real backend). This file must not
request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import os
import pathlib
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


def test_resolve_exec_start_falls_back_to_the_module_form_without_a_shim(tmp_path):
    exec_start = resolve_exec_start(tmp_path)
    assert exec_start.endswith("-m agent_os.guard tick")
    assert "scripts/agent_guard.py" not in exec_start


def _fake_interpreter(directory):
    directory.mkdir(parents=True, exist_ok=True)
    python = directory / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return python


def _no_mechanism_interpreter(monkeypatch, tmp_path):
    """`agent_os_python()` with nothing to find: no `$AGENT_OS_PYTHON` and a package directory
    with no `.venv` beside it, so its last step is the bare `python3` fallback."""
    import agent_os.cli

    monkeypatch.delenv("AGENT_OS_PYTHON", raising=False)
    monkeypatch.setattr(agent_os.cli, "AGENT_OS_DIR", tmp_path / "unbootstrapped")


@pytest.mark.parametrize("host_venv", [True, False], ids=["host-venv", "no-host-venv"])
def test_resolve_exec_start_runs_the_shim_on_the_mechanisms_interpreter(
    tmp_path, monkeypatch, host_venv
):
    """The shim runs on the mechanism's own interpreter whether or not the host has a root
    `.venv`. Without one, #12: the unit used to render a bare `python3`, which only works if the
    systemd `--user` manager's PATH happens to carry one with the mechanism's dependencies. With
    one, #51: the unit used to prefer it, so the host's package versions decided how the guard
    behaved -- against AGENT_OS.md §8's "never a host's"."""
    (tmp_path / "scripts").mkdir()
    shim = tmp_path / "scripts" / "agent_guard.py"
    shim.write_text("# shim\n")
    if host_venv:
        _fake_interpreter(tmp_path / ".venv" / "bin")
    mechanism_python = _fake_interpreter(tmp_path / "mechanism" / ".venv" / "bin")
    monkeypatch.setenv("AGENT_OS_PYTHON", str(mechanism_python))

    exec_start = resolve_exec_start(tmp_path)
    assert exec_start == f"{mechanism_python} {shim} tick"
    assert str(tmp_path / ".venv") not in exec_start


@pytest.mark.parametrize("with_shim", [True, False], ids=["shim", "module-form"])
def test_resolve_exec_start_refuses_when_no_absolute_interpreter_resolves(
    tmp_path, monkeypatch, with_shim
):
    if with_shim:
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "agent_guard.py").write_text("# shim\n")
    _no_mechanism_interpreter(monkeypatch, tmp_path)

    with pytest.raises(InstallError, match="bootstrap.sh"):
        resolve_exec_start(tmp_path)


def test_resolve_exec_start_refuses_a_bare_name_in_agent_os_python(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OS_PYTHON", "python3")
    with pytest.raises(InstallError, match="AGENT_OS_PYTHON"):
        resolve_exec_start(tmp_path)


def test_resolve_exec_start_uses_the_venv_bootstrap_builds_beside_the_package(
    tmp_path, monkeypatch
):
    import agent_os.cli

    package_dir = tmp_path / "agent_os_dir"
    mechanism_python = _fake_interpreter(package_dir / ".venv" / "bin")
    monkeypatch.delenv("AGENT_OS_PYTHON", raising=False)
    monkeypatch.setattr(agent_os.cli, "AGENT_OS_DIR", package_dir)

    exec_start = resolve_exec_start(tmp_path)
    assert exec_start == f"{mechanism_python} -m agent_os.guard tick"


def test_install_exits_loudly_when_no_interpreter_resolves(tmp_path):
    """End to end: the rendered unit is never written with a bare `python3` -- `main()` exits
    non-zero naming the fix instead, and nothing lands under `~/.config/systemd/user/`."""
    environment, _host_root, fake_home = _isolated_environment(tmp_path)
    environment["AGENT_OS_PYTHON"] = "python3"

    result = _run_install(environment)
    assert result.returncode != 0
    assert "AGENT_OS_PYTHON" in result.stderr and "bootstrap.sh" in result.stderr, result.stderr
    assert not (fake_home / ".config" / "systemd").exists()


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


def test_action_reports_would_create_for_an_absent_file_only_under_dry_run(tmp_path):
    action = Action(tmp_path / "new.txt", "content\n")
    assert action.status(force=False, dry_run=True) == "would create"
    assert action.status(force=False, dry_run=False) == "created"
    assert action.should_write(force=False)


def test_action_reports_an_empty_file_in_both_modes(tmp_path):
    action = Action(tmp_path / "empty.txt", "")
    assert action.status(force=False, dry_run=True) == "would create (empty)"
    assert action.status(force=False, dry_run=False) == "created (empty)"


def test_action_reports_up_to_date_and_skips_when_content_matches(tmp_path):
    path = tmp_path / "same.txt"
    path.write_text("content\n")
    action = Action(path, "content\n")
    for dry_run in (True, False):
        assert "up to date -- skipped" in action.status(force=False, dry_run=dry_run)
    assert not action.should_write(force=False)


def test_action_refuses_to_overwrite_a_differing_file_without_force(tmp_path):
    path = tmp_path / "differs.txt"
    path.write_text("old\n")
    action = Action(path, "new\n")
    for dry_run in (True, False):
        assert "refusing without --force" in action.status(force=False, dry_run=dry_run)
    assert not action.should_write(force=False)
    assert "-old" in action.diff and "+new" in action.diff


def test_action_overwrites_a_differing_file_with_force(tmp_path):
    path = tmp_path / "differs.txt"
    path.write_text("old\n")
    action = Action(path, "new\n")
    assert "would overwrite (--force)" in action.status(force=True, dry_run=True)
    assert "overwritten (--force)" in action.status(force=True, dry_run=False)
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


def test_main_writes_a_host_ci_workflow_that_runs_the_configured_test_command(tmp_path):
    # agent-os#50: with only the path-filtered ci-agent-os.yml, a host-only PR reports no check
    # at all, and the control plane's merge condition 1 treats that as not met.
    environment, host_root, _fake_home = _isolated_environment(tmp_path)

    result = _run_install(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    workflow = host_root / ".github" / "workflows" / "ci-host.yml"
    assert "run: scripts/test.sh\n" in workflow.read_text()


def test_main_leaves_a_hosts_own_differing_ci_workflow_alone_without_force(tmp_path):
    environment, host_root, _fake_home = _isolated_environment(tmp_path)
    workflow = host_root / ".github" / "workflows" / "ci-host.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: the host's own\n")

    result = _run_install(environment)
    assert result.returncode != 0
    assert workflow.read_text() == "name: the host's own\n"


def test_main_without_dry_run_reports_what_it_wrote_in_the_past_tense(tmp_path):
    # agent-os#9: a real run printed "would create" for every file it then created, which reads
    # exactly like a dry run. Only `--dry-run` may speak in the conditional.
    environment, _host_root, fake_home = _isolated_environment(tmp_path)

    result = _run_install(environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would" not in result.stdout, result.stdout
    service = fake_home / ".config" / "systemd" / "user" / "acme-guard.service"
    assert f"{service}: created\n" in result.stdout, result.stdout


def test_main_force_reports_the_overwrite_in_the_mode_it_ran_in(tmp_path):
    environment, _host_root, fake_home = _isolated_environment(tmp_path)
    assert _run_install(environment).returncode == 0
    service = fake_home / ".config" / "systemd" / "user" / "acme-guard.service"
    service.write_text("hand-edited\n")

    dry = _run_install(environment, "--dry-run", "--force")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert f"{service}: exists and differs -- would overwrite (--force)\n" in dry.stdout
    assert service.read_text() == "hand-edited\n", "a dry run must write nothing"

    real = _run_install(environment, "--force")
    assert real.returncode == 0, real.stdout + real.stderr
    assert f"{service}: exists and differs -- overwritten (--force)\n" in real.stdout
    assert "would" not in real.stdout, real.stdout


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


@pytest.mark.parametrize("name", ["guard.service", "guard.timer", "override.conf"])
def test_rendered_units_name_only_docs_the_mechanism_ships(tmp_path, name):
    # agent-os#10: a rendered unit lands on a host that has only `agent_os/`; a comment pointing at
    # a host's own runbook is a dead reference there.
    import re

    rendered = render_systemd_unit(name, _project(), tmp_path)
    named = re.findall(r"(?:agent_os/)?docs/[\w./-]+\.md", rendered)
    missing = [p for p in named if not (AGENT_OS_DIR / p.removeprefix("agent_os/")).is_file()]
    assert missing == [], rendered


# `config/agents.yaml` absent or broken (agent-os#3): a one-line refusal naming the file and the
# adoption step that writes it, never a traceback out of `load_agents_config`.


def test_main_reports_a_missing_config_without_a_traceback(tmp_path):
    environment, _host_root, _fake_home = _isolated_environment(tmp_path)
    environment["AGENTS_CONFIG_PATH"] = str(tmp_path / "absent.yaml")
    result = _run_install(environment, "--dry-run")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    assert str(tmp_path / "absent.yaml") in result.stderr
    assert "ADOPTION.md step 8" in result.stderr


@pytest.mark.parametrize(
    "text",
    ["project: [unclosed\n", "project:\n  repo: owner/name\n  no_such_key: 1\n"],
    ids=["yaml-syntax", "schema"],
)
def test_main_reports_an_invalid_config_without_a_traceback(tmp_path, text):
    environment, _host_root, _fake_home = _isolated_environment(tmp_path)
    pathlib.Path(environment["AGENTS_CONFIG_PATH"]).write_text(text)
    result = _run_install(environment, "--dry-run")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    assert environment["AGENTS_CONFIG_PATH"] in result.stderr
    assert "does not load" in result.stderr
