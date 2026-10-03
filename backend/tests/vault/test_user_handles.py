"""The vault handle: a root one on the repository, a user one on that user's folder."""

from __future__ import annotations

from pathlib import Path

import pytest
from user_helpers import add_user, everything_under, user_directory

from studentassistant.vault import UserError, UserNotFoundError, Vault, VaultError
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME

USER_ID = "ana-garcia"
OTHER_USER_ID = "luis-martin"
NAME = "Ana García"


def test_a_vault_just_created_or_read_back_is_a_root_handle_on_the_repository(
    tmp_vault: Vault, tmp_path: Path
) -> None:
    assert tmp_vault.root == tmp_vault.path == tmp_path / "vault"
    assert tmp_vault.user_id is None

    opened = Vault.open(tmp_vault.path)

    assert opened.root == opened.path == tmp_vault.root
    assert opened.user_id is None
    assert opened == tmp_vault, "the same directory read back is the same handle"


def test_init_creates_no_user_folder_of_its_own(tmp_vault: Vault) -> None:
    assert not (tmp_vault.root / USERS_DIRNAME).exists()


def test_for_user_gives_a_handle_on_the_users_folder_of_the_same_repository(
    tmp_vault: Vault,
) -> None:
    add_user(tmp_vault, USER_ID, NAME)

    user = tmp_vault.for_user(USER_ID)

    assert user.path == user.root / USERS_DIRNAME / USER_ID == user_directory(tmp_vault, USER_ID)
    assert user.root == tmp_vault.root, "the repository is the same one; only the content moves"
    assert user.meta == tmp_vault.meta
    assert user.user_id == USER_ID


def test_the_handle_a_root_handle_gives_leaves_the_root_handle_as_it_was(tmp_vault: Vault) -> None:
    add_user(tmp_vault, USER_ID, NAME)
    before = everything_under(tmp_vault.root)

    tmp_vault.for_user(USER_ID)

    assert (tmp_vault.path, tmp_vault.user_id) == (tmp_vault.root, None)
    assert everything_under(tmp_vault.root) == before, "opening a user's handle writes nothing"


def test_a_vault_read_back_gives_the_handle_of_the_same_user(tmp_vault: Vault) -> None:
    add_user(tmp_vault, USER_ID, NAME)

    assert Vault.open(tmp_vault.path).for_user(USER_ID) == tmp_vault.for_user(USER_ID)


def test_for_user_refuses_a_user_that_is_not_there(tmp_vault: Vault) -> None:
    with pytest.raises(UserNotFoundError):
        tmp_vault.for_user(USER_ID)


def test_for_user_refuses_a_folder_that_holds_no_profile(tmp_vault: Vault) -> None:
    user_directory(tmp_vault, USER_ID).mkdir(parents=True)

    with pytest.raises(UserNotFoundError):
        tmp_vault.for_user(USER_ID)
    assert not (user_directory(tmp_vault, USER_ID) / USER_PROFILE_NAME).exists()


@pytest.mark.parametrize(
    "user_id",
    ["", "..", ".", "../ana-garcia", "ana-garcia/../../etc", "Ana García", "ana_garcia", "-ana-"],
)
def test_for_user_refuses_an_id_that_is_not_a_slug(tmp_vault: Vault, user_id: str) -> None:
    with pytest.raises(UserNotFoundError):
        tmp_vault.for_user(user_id)


def test_for_user_refuses_a_not_a_slug_id_even_when_the_folder_it_names_exists(
    tmp_vault: Vault,
) -> None:
    add_user(tmp_vault, USER_ID, NAME)
    walking = f"{USER_ID}/../{USER_ID}"
    assert (tmp_vault.root / USERS_DIRNAME / walking / USER_PROFILE_NAME).is_file()

    with pytest.raises(UserNotFoundError):
        tmp_vault.for_user(walking)


def test_refusing_an_id_leaves_the_disk_as_it_was(tmp_vault: Vault, tmp_path: Path) -> None:
    before = everything_under(tmp_path)

    with pytest.raises(UserNotFoundError):
        tmp_vault.for_user("../outside")

    assert everything_under(tmp_path) == before


def test_for_user_on_a_user_handle_is_refused(tmp_vault: Vault) -> None:
    add_user(tmp_vault, USER_ID, NAME)
    add_user(tmp_vault, OTHER_USER_ID, "Luis Martín")
    user = tmp_vault.for_user(USER_ID)

    with pytest.raises(ValueError):
        user.for_user(OTHER_USER_ID)
    with pytest.raises(ValueError):
        user.for_user(USER_ID)


def test_a_user_refusal_is_a_vault_refusal() -> None:
    assert issubclass(UserError, VaultError)
    assert issubclass(UserNotFoundError, UserError)
