"""The fixtures any backend test can ask for, wherever in `tests/` it lives."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from studentassistant.install import service
from studentassistant.vault import Vault

# The name a test vault records in its `vault.yaml`; tests that assert on it import it from here.
STUDENT = "Ana García"


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """An initialised vault under `tmp_path`, for any test that needs somewhere to write content.

    `HOME` is moved into `tmp_path` first, because creating a vault runs `git init` and git reads
    the global configuration of whoever runs it: a test must neither depend on the student's home
    directory nor write into it. `XDG_CONFIG_HOME` goes with it, since git looks there before it
    looks in `~/.gitconfig`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return Vault.init(tmp_path / "vault", student=STUDENT)


@pytest.fixture
def git_origin(tmp_path: Path, tmp_vault: Vault) -> Path:
    """A local bare repository registered as `origin` of `tmp_vault`: the "GitHub" of the tests.

    It lives under `tmp_path`, so pushing to it and pulling from it never touches the network.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=tmp_vault.path,
        check=True,
        capture_output=True,
    )
    return origin


class FakeSystemctl:
    """Stands in for `systemctl --user`: records every call, answers from `answers`.

    `answers` maps a subcommand (`is-active`, `enable`...) to the `SystemctlResult` it returns;
    anything else succeeds with no output, and `is-active` answers `active` unless told otherwise.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers: dict[str, service.SystemctlResult] = {
            "is-active": service.SystemctlResult(ok=True, output="active")
        }

    def __call__(self, *args: str, timeout: float = 0) -> service.SystemctlResult:
        self.calls.append(args)
        return self.answers.get(args[0], service.SystemctlResult(ok=True, output=""))


@pytest.fixture(autouse=True)
def systemctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> FakeSystemctl:
    """No test ever talks to this machine's systemd or writes into its unit directory.

    Every test gets a `FakeSystemctl` in place of `install.service.run_systemctl`, and a unit
    directory of its own under pytest's temporary root (created only when a test asks for it).
    """
    fake = FakeSystemctl()
    monkeypatch.setattr(service, "run_systemctl", fake)
    units: list[Path] = []

    def unit_directory() -> Path:
        if not units:
            units.append(tmp_path_factory.mktemp("systemd-user"))
        return units[0]

    monkeypatch.setattr(service, "unit_directory", unit_directory)
    return fake
