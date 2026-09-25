"""The systemd `--user` unit: rendered with `SA_CONFIG`, written once, enabled and started."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from studentassistant.install import service
from studentassistant.install.service import SystemctlResult, render_unit
from studentassistant.install.service import run_systemctl as real_run_systemctl
from studentassistant.install.service import unit_directory as real_unit_directory


def test_the_unit_runs_serve_with_the_config_pinned() -> None:
    text = render_unit(Path("/opt/sa/bin/studentassistant"), Path("/home/ana/.config/sa.toml"))

    assert "ExecStart=/opt/sa/bin/studentassistant serve\n" in text
    assert "Environment=SA_CONFIG=/home/ana/.config/sa.toml\n" in text
    assert "Restart=on-failure\n" in text
    assert "WantedBy=default.target\n" in text


def test_paths_with_spaces_are_quoted() -> None:
    text = render_unit(Path("/opt/my apps/studentassistant"), Path("/home/ana/100% conf.toml"))

    assert 'ExecStart="/opt/my apps/studentassistant" serve\n' in text
    assert 'Environment="SA_CONFIG=/home/ana/100%% conf.toml"\n' in text


def test_install_writes_the_unit_then_enables_and_starts_it(systemctl, tmp_path: Path) -> None:
    result = service.install_unit(tmp_path / "studentassistant", tmp_path / "config.toml")

    assert result.error is None
    assert result.changed is True
    assert result.path == service.unit_path()
    assert result.path.read_text(encoding="utf-8") == render_unit(
        tmp_path / "studentassistant", tmp_path / "config.toml"
    )
    assert systemctl.calls == [("daemon-reload",), ("enable", "--now", service.UNIT_NAME)]


def test_reinstalling_the_same_unit_changes_nothing(systemctl, tmp_path: Path) -> None:
    service.install_unit(tmp_path / "sa", tmp_path / "config.toml")
    systemctl.calls.clear()

    again = service.install_unit(tmp_path / "sa", tmp_path / "config.toml")

    assert again.changed is False
    assert systemctl.calls == [("daemon-reload",), ("enable", "--now", service.UNIT_NAME)]


def test_a_changed_unit_restarts_the_running_service(systemctl, tmp_path: Path) -> None:
    service.install_unit(tmp_path / "sa", tmp_path / "config.toml")
    systemctl.calls.clear()

    changed = service.install_unit(tmp_path / "sa", tmp_path / "other.toml")

    assert changed.changed is True
    assert systemctl.calls[-1] == ("try-restart", service.UNIT_NAME)


def test_a_systemctl_failure_is_reported(systemctl, tmp_path: Path) -> None:
    systemctl.answers["enable"] = SystemctlResult(ok=False, output="Failed to connect to bus")

    result = service.install_unit(tmp_path / "sa", tmp_path / "config.toml")

    assert result.error == (
        f"systemctl --user enable --now {service.UNIT_NAME} falló: Failed to connect to bus"
    )


def test_service_state_is_what_is_active_says(systemctl) -> None:
    assert service.service_state() == "active"
    systemctl.answers["is-active"] = SystemctlResult(ok=False, output="inactive")
    assert service.service_state() == "inactive"


def test_unit_directory_follows_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert real_unit_directory() == tmp_path / "xdg" / "systemd" / "user"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert real_unit_directory() == tmp_path / "home" / ".config" / "systemd" / "user"


def test_run_systemctl_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse `systemctl` fixture replaced `service.run_systemctl`; the name imported at the
    # top of this module is the real one, and `subprocess.run` is replaced so nothing runs.
    def missing(*args, **kwargs):
        raise FileNotFoundError("systemctl")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("systemctl", 1)

    monkeypatch.setattr(subprocess, "run", missing)
    result = real_run_systemctl("is-active", "x")
    assert result.ok is False
    assert "no se pudo ejecutar systemctl" in result.output

    monkeypatch.setattr(subprocess, "run", slow)
    result = real_run_systemctl("is-active", "x")
    assert result.ok is False
    assert "no respondió a tiempo" in result.output
