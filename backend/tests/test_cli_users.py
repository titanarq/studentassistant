"""`studentassistant users list|add`: the students of the vault, from the maintainer's shell (#549).

What a user is in the vault -- the folder, the profile, the id rule, the photo -- is
`tests/vault/test_vault_users.py` and `tests/vault/test_user_photo.py`, and what the API does with
them is `tests/server/test_user_routes.py`. These are about the command: the Spanish it prints,
what it exits with, that it commits nothing, and that a service holding the vault's git lock does
not stop it. Every test points the CLI at a vault under `tmp_path` through `SA_CONFIG` and
`SA_VAULT__PATH`, so none of them reads this machine's configuration or its vault.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.server.user_routes import DAMAGED_PROFILE_DETAIL, USER_EXISTS_DETAIL
from studentassistant.vault import (
    Vault,
    create_user,
    get_user,
    list_users,
    set_user_photo,
    user_ids,
)
from studentassistant.vault.locking import git_lock
from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.users import GITKEEP_NAME
from studentassistant.vault.vault import MIGRATE_USERS_COMMAND, USER_PROFILE_NAME, USERS_DIRNAME

# The student `tests/conftest.py`'s `tmp_vault` is born with, and the id derived from that name
# (#548), spelled out here: in a full run `from conftest import` resolves to whichever
# `tests/*/conftest.py` was collected last, which is not the shared one.
STUDENT = "Ana García"
STUDENT_USER_ID = "ana-garcia"
CLASSMATE_NAME = "Adrián Luna"
"""A classmate whose name sorts before the vault's first student's, so a listing shows its order."""
CLASSMATE_EMAIL = "adrian.luna@instituto.es"
CLASSMATE = "adrian-luna"
FIRST_EMAIL = "ana.garcia@instituto.es"
NOBODY = "Todavía no hay ningún usuario"


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration that is not this machine's: `SA_CONFIG` in `tmp_path`, no other `SA_*`."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    return tmp_path


@pytest.fixture
def vault(configured: Path, tmp_vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """The vault every command here reads: `tmp_vault`, born with its first student in it."""
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    return tmp_vault


@pytest.fixture
def empty_vault(
    configured: Path, vault_with_no_users: Vault, monkeypatch: pytest.MonkeyPatch
) -> Vault:
    """A vault nobody belongs to yet, which is what `users add` works with on a fresh PC."""
    monkeypatch.setenv("SA_VAULT__PATH", str(vault_with_no_users.path))
    return vault_with_no_users


def run(*args: str):
    return CliRunner().invoke(cli, ["users", *args])


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` for a test's own inspection."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def commits(root: Path) -> int:
    """How many commits `root` has.

    `Vault.init` makes none, so `HEAD` has nothing to say yet and every ref is counted instead.
    """
    return int(git(root, "rev-list", "--count", "--all").strip() or 0)


def profile_file(vault: Vault, user_id: str) -> Path:
    return vault.root / USERS_DIRNAME / user_id / USER_PROFILE_NAME


def portrait() -> bytes:
    """A real JPEG, drawn and never photographed: the least `set_user_photo` decodes."""
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    image[:, :, 1] = 160
    encoded_ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert encoded_ok, "the test's own image must encode"
    return buffer.tobytes()


def test_list_shows_one_line_per_student_by_name(vault: Vault) -> None:
    create_user(vault, CLASSMATE_NAME, CLASSMATE_EMAIL)

    result = run("list")

    assert result.exit_code == 0, result.output
    # The classmate was added second and is listed first: a listing reads by name.
    assert result.output.splitlines() == [
        f"{CLASSMATE}\t{CLASSMATE_NAME}\t{CLASSMATE_EMAIL}\tsin foto",
        f"{STUDENT_USER_ID}\t{STUDENT}\tsin email\tsin foto",
    ]


def test_list_says_who_has_a_photo(vault: Vault) -> None:
    set_user_photo(vault, STUDENT_USER_ID, portrait(), "image/jpeg")

    result = run("list")

    assert result.exit_code == 0, result.output
    assert f"{STUDENT_USER_ID}\t{STUDENT}\tsin email\tcon foto" in result.output


def test_list_json_holds_the_profiles_as_the_vault_stores_them(vault: Vault) -> None:
    create_user(vault, CLASSMATE_NAME, CLASSMATE_EMAIL)

    result = run("list", "--json")

    assert result.exit_code == 0, result.output
    listed = json.loads(result.output)
    assert [user["id"] for user in listed] == [CLASSMATE, STUDENT_USER_ID]
    assert listed[0]["name"] == CLASSMATE_NAME
    assert listed[0]["email"] == CLASSMATE_EMAIL
    assert listed[0]["photo"] is None
    created_at = datetime.fromisoformat(listed[0]["created_at"])
    assert created_at == get_user(vault, CLASSMATE).created_at, "the instant the profile records"
    assert listed[1]["email"] is None, "a student with no address keeps saying so in JSON too"


def test_list_a_vault_nobody_belongs_to_yet(empty_vault: Vault) -> None:
    result = run("list")

    assert result.exit_code == 0, result.output
    assert NOBODY in result.output
    assert "users add --name" in result.output, "the way out of it is named"
    assert run("list", "--json").output.strip() == "[]"


def test_add_prints_the_new_id_and_creates_the_folder(vault: Vault) -> None:
    result = run("add", "--name", CLASSMATE_NAME, "--email", CLASSMATE_EMAIL)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == CLASSMATE, "the id alone, so a script can take it"
    created = get_user(vault, CLASSMATE)
    assert (created.name, created.email, created.photo) == (CLASSMATE_NAME, CLASSMATE_EMAIL, None)
    assert profile_file(vault, CLASSMATE).is_file()
    assert (vault.root / USERS_DIRNAME / CLASSMATE / SUBJECTS_DIRNAME / GITKEEP_NAME).is_file()


def test_add_the_first_student_to_a_vault_with_nobody(empty_vault: Vault) -> None:
    result = run("add", "--name", STUDENT, "--email", FIRST_EMAIL)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == STUDENT_USER_ID
    assert get_user(empty_vault, STUDENT_USER_ID).email == FIRST_EMAIL


def test_add_a_second_student_with_the_same_name_gets_its_own_id(vault: Vault) -> None:
    result = run("add", "--name", STUDENT, "--email", FIRST_EMAIL)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{STUDENT_USER_ID}-2"
    assert user_ids(vault) == [STUDENT_USER_ID, f"{STUDENT_USER_ID}-2"]
    assert get_user(vault, f"{STUDENT_USER_ID}-2").email == FIRST_EMAIL
    assert get_user(vault, STUDENT_USER_ID).email is None, "the first student is untouched"


def test_add_commits_nothing_and_leaves_the_folder_to_the_service(vault: Vault) -> None:
    before = commits(vault.root)

    result = run("add", "--name", CLASSMATE_NAME)

    assert result.exit_code == 0, result.output
    assert commits(vault.root) == before
    assert git(vault.root, "ls-files", f"users/{CLASSMATE}").strip() == "", "nor stages it"


def test_a_name_the_vault_refuses_exits_1_and_writes_nothing(vault: Vault) -> None:
    refused = (
        ("   ", "El nombre no puede estar vacío."),
        ("???", "no sirve para identificar a un usuario"),
        ("a" * 81, "El nombre es demasiado largo"),
    )

    for name, said in refused:
        result = run("add", "--name", name)
        assert result.exit_code == 1, result.output
        assert said in result.output

    assert user_ids(vault) == [STUDENT_USER_ID]


def test_an_email_the_vault_refuses_exits_1_and_writes_nothing(vault: Vault) -> None:
    result = run("add", "--name", CLASSMATE_NAME, "--email", "adrian@instituto")

    assert result.exit_code == 1
    assert "La dirección de correo «adrian@instituto» no es válida" in result.output
    assert user_ids(vault) == [STUDENT_USER_ID]


def test_add_without_a_name_is_a_usage_error(vault: Vault) -> None:
    assert run("add").exit_code == 2
    assert run("add", "--nombre", CLASSMATE_NAME).exit_code == 2


def test_a_folder_that_is_not_a_users_is_not_written_over(vault: Vault) -> None:
    """The id is taken by something that is not a folder: `create_user` refuses it instead of
    writing over it, which is what two `users add` at once look like from the second one."""
    taken = vault.root / USERS_DIRNAME / CLASSMATE
    taken.write_text("no soy una carpeta", encoding="utf-8")

    result = run("add", "--name", CLASSMATE_NAME)

    assert result.exit_code == 1
    assert USER_EXISTS_DETAIL in result.output
    assert taken.read_text(encoding="utf-8") == "no soy una carpeta"


def test_a_profile_it_cannot_read_back_exits_1(vault: Vault) -> None:
    """A damaged `profile.json` is the student's to recover, so the listing stops instead of
    leaving them out of it (`list_users` raises rather than skipping)."""
    profile_file(vault, STUDENT_USER_ID).write_text("{esto no es json", encoding="utf-8")

    for args in (["list"], ["list", "--json"]):
        result = run(*args)
        assert result.exit_code == 1, result.output
        assert DAMAGED_PROFILE_DETAIL in result.output


def test_add_still_works_with_a_profile_it_cannot_read(vault: Vault) -> None:
    """Picking an id counts folders and reads no profile, so a damaged one blocks no arrival."""
    profile_file(vault, STUDENT_USER_ID).write_text("{esto no es json", encoding="utf-8")

    result = run("add", "--name", CLASSMATE_NAME)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == CLASSMATE


def test_a_vault_that_needs_migrating_exits_1_naming_the_migration(
    configured: Path, legacy_vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_VAULT__PATH", str(legacy_vault.root))

    for args in (["list"], ["add", "--name", CLASSMATE_NAME]):
        result = run(*args)
        assert result.exit_code == 1, result.output
        assert "No se puede abrir la bóveda" in result.output
        assert MIGRATE_USERS_COMMAND in result.output


def test_a_missing_vault_exits_1(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_VAULT__PATH", str(configured / "nope"))

    for args in (["list"], ["add", "--name", CLASSMATE_NAME]):
        result = run(*args)
        assert result.exit_code == 1
        assert "No se puede abrir la bóveda" in result.output


def test_both_commands_work_while_the_service_holds_the_vault(vault: Vault) -> None:
    """Neither waits on `serve`: the git lock is held, as it is while the service commits.

    A user is ordinary vault content, written through the vault's atomic writers and no lock of
    its own, so a student can be added to a backend that is capturing; the service's next batch
    commit stages the folder with everything else (`git add --all`).
    """
    lock_path = git_lock(vault.root).path
    assert lock_path is not None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Another process (the running service) holding it: a second open file description's flock.
    with lock_path.open("a") as service:
        fcntl.flock(service, fcntl.LOCK_EX)
        listed = run("list")
        added = run("add", "--name", CLASSMATE_NAME)

    assert listed.exit_code == 0, listed.output
    assert added.exit_code == 0, added.output
    assert added.output.strip() == CLASSMATE
    assert [profile.id for profile in list_users(vault)] == [CLASSMATE, STUDENT_USER_ID]
