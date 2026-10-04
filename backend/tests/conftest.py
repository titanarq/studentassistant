"""The fixtures any backend test can ask for, wherever in `tests/` it lives."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from studentassistant.install import service
from studentassistant.vault import Vault
from studentassistant.vault.files import dump_yaml, write_text_atomic
from studentassistant.vault.models import LEGACY_FORMAT_VERSION
from studentassistant.vault.vault import USERS_DIRNAME, VAULT_META_NAME

# The name a test vault records in its `vault.yaml`; tests that assert on it import it from here.
STUDENT = "Ana García"
# The id of the user every test vault is born with, which is `slugify(STUDENT)` (#548).
STUDENT_USER_ID = "ana-garcia"


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """An initialised vault under `tmp_path`, for any test that needs somewhere to write content.

    `HOME` is moved into `tmp_path` first, because creating a vault runs `git init` and git reads
    the global configuration of whoever runs it: a test must neither depend on the student's home
    directory nor write into it. `XDG_CONFIG_HOME` goes with it, since git looks there before it
    looks in `~/.gitconfig`.

    The handle is the ROOT one, `Vault.init`'s own: the repository, whose `users/` holds the first
    user, `STUDENT_USER_ID`. A test that writes one student's content asks for
    `tmp_vault.for_user(STUDENT_USER_ID)`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return Vault.init(tmp_path / "vault", student=STUDENT)


@pytest.fixture
def vault_with_no_users(tmp_vault: Vault) -> Vault:
    """`tmp_vault` with the user it was born with taken out: a vault nobody belongs to yet.

    `Vault.init` creates the first user (#548), so the state these tests ask about -- what
    `list_users` says of a vault with nobody in it, which id the first `create_user` picks -- no
    longer comes with a vault. Removing `users/` is what a repository an older backend wrote looks
    like, and nothing else about it changes.
    """
    shutil.rmtree(tmp_vault.root / USERS_DIRNAME)
    return tmp_vault


@pytest.fixture
def legacy_vault(vault_with_no_users: Vault) -> Vault:
    """A format-1 vault: its content at the repository root, no user, opened for migration.

    `Vault.open` refuses a vault like this one and names the command that migrates it, so the
    handle handed out is `Vault.open_for_migration`'s -- the only one that opens it. Its
    `vault.yaml` holds the three fields the format-1 layout had, and no `legacy_root_user`: that
    field is what format 2 writes to say who received the root's content (#548).
    """
    write_text_atomic(
        vault_with_no_users.root / VAULT_META_NAME,
        dump_yaml(
            {
                "format_version": LEGACY_FORMAT_VERSION,
                "created_at": vault_with_no_users.meta.created_at.isoformat(),
                "student": STUDENT,
            }
        ),
    )
    return Vault.open_for_migration(vault_with_no_users.root)


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
