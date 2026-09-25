"""The systemd `--user` unit that keeps `studentassistant serve` running on this PC.

`install_unit` writes `studentassistant.service` into the user's unit directory
(`$XDG_CONFIG_HOME/systemd/user`, by default `~/.config/systemd/user`), reloads systemd and
enables and starts it; re-running it with nothing changed only makes sure it is enabled and
running. Every `systemctl` call goes through `run_systemctl`, which the tests replace, so no test
ever talks to the real systemd.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "studentassistant.service"
SYSTEMCTL_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class SystemctlResult:
    """What one `systemctl --user` call answered."""

    ok: bool
    output: str


def run_systemctl(*args: str, timeout: float = SYSTEMCTL_TIMEOUT_SECONDS) -> SystemctlResult:
    """Run `systemctl --user <args>`; never raises (a missing systemctl is a failed result)."""
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return SystemctlResult(ok=False, output=f"systemctl --user {args[0]} no respondió a tiempo")
    except OSError as error:
        return SystemctlResult(ok=False, output=f"no se pudo ejecutar systemctl: {error}")
    output = (completed.stdout or completed.stderr).strip()
    return SystemctlResult(ok=completed.returncode == 0, output=output)


def unit_directory() -> Path:
    """The user's systemd unit directory."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_path() -> Path:
    return unit_directory() / UNIT_NAME


def serve_executable() -> Path:
    """The `studentassistant` console script of the running installation."""
    beside = Path(sys.executable).parent / "studentassistant"
    if beside.exists():
        return beside.absolute()
    found = shutil.which("studentassistant")
    if found is None:
        raise FileNotFoundError("the studentassistant command is not installed on the PATH")
    return Path(found).absolute()


def _quote(value: str) -> str:
    """A systemd command-line or assignment word, quoted when it needs to be."""
    if value and not any(character in value for character in " \t\"'\\$%;"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_unit(executable: Path, config_path: Path) -> str:
    """The unit's text: `serve` with `SA_CONFIG` pinned, restarted when it fails."""
    return (
        "[Unit]\n"
        "Description=Student Assistant backend (studentassistant serve)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=exec\n"
        f"ExecStart={_quote(str(executable))} serve\n"
        f"Environment={_quote(f'SA_CONFIG={config_path}')}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


@dataclass(frozen=True)
class InstallResult:
    """What `install_unit` did: the unit's path, whether it changed, and systemd's failure."""

    path: Path
    changed: bool
    error: str | None = None


def install_unit(executable: Path, config_path: Path) -> InstallResult:
    """Write the unit (when it differs), reload systemd, then enable and (re)start the service."""
    path = unit_path()
    text = render_unit(executable, config_path)
    existed = path.exists()
    changed = not existed or path.read_text(encoding="utf-8") != text
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    steps: list[tuple[str, ...]] = [("daemon-reload",), ("enable", "--now", UNIT_NAME)]
    if changed and existed:
        # An already-running service keeps the old unit until it is restarted.
        steps.append(("try-restart", UNIT_NAME))
    for step in steps:
        result = run_systemctl(*step)
        if not result.ok:
            detail = f": {result.output}" if result.output else ""
            return InstallResult(path, changed, f"systemctl --user {' '.join(step)} falló{detail}")
    return InstallResult(path, changed)


def service_state() -> str:
    """`systemctl --user is-active` for the unit: `active`, `inactive`, `failed`, ... or why not."""
    result = run_systemctl("is-active", UNIT_NAME)
    return result.output or ("active" if result.ok else "desconocido")
