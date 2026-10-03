"""The users of the vault: creating them, listing them, reading and editing a profile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from git_helpers import git
from user_helpers import add_user, everything_under, user_directory

from studentassistant import vault as vault_package
from studentassistant.vault import (
    UserError,
    UserFileError,
    UserNotFoundError,
    UserProfile,
    UserProfileError,
    Vault,
    VaultError,
    create_subject,
    create_user,
    get_user,
    list_subjects,
    list_users,
    subject_slugs,
    update_user,
    user_ids,
)
from studentassistant.vault.files import write_json_atomic
from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.users import (
    GITKEEP_NAME,
    MAX_USER_EMAIL_CHARACTERS,
    MAX_USER_NAME_CHARACTERS,
)
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME

NAME = "Ana García"
EMAIL = "ana.garcia@instituto.es"


def profile_file(vault: Vault, user_id: str) -> Path:
    """The `profile.json` of a user, whether or not anything has written it yet."""
    return user_directory(vault, user_id) / USER_PROFILE_NAME


def write_profile(vault: Vault, profile: UserProfile) -> None:
    """Put `profile` in its user's folder the way `create_user` and `update_user` would."""
    write_json_atomic(profile_file(vault, profile.id), profile)


def test_a_vault_that_has_no_user_yet_lists_none_and_has_no_users_directory(
    tmp_vault: Vault,
) -> None:
    assert list_users(tmp_vault) == []
    assert user_ids(tmp_vault) == []
    assert not (tmp_vault.root / USERS_DIRNAME).exists()


def test_creating_a_user_writes_its_folder_its_profile_and_its_subjects(tmp_vault: Vault) -> None:
    profile = create_user(tmp_vault, NAME, email=EMAIL)
    directory = user_directory(tmp_vault, profile.id)
    on_disk = json.loads(profile_file(tmp_vault, profile.id).read_text(encoding="utf-8"))

    assert profile.id == "ana-garcia"
    assert profile.name == NAME
    assert profile.email == EMAIL
    assert profile.photo is None
    assert directory.is_dir()
    assert on_disk["id"] == "ana-garcia"
    assert on_disk["name"] == NAME
    assert on_disk["email"] == EMAIL
    assert on_disk["photo"] is None
    assert UserProfile.model_validate(on_disk) == profile, "created_at comes back as it went"
    assert (directory / SUBJECTS_DIRNAME / GITKEEP_NAME).is_file()


def test_a_profile_is_written_with_its_fields_in_the_order_the_layout_shows_them(
    tmp_vault: Vault,
) -> None:
    profile = create_user(tmp_vault, NAME)

    text = profile_file(tmp_vault, profile.id).read_text(encoding="utf-8")

    assert list(json.loads(text)) == list(UserProfile.model_fields)
    assert text.endswith("\n")


def test_a_created_user_is_the_one_for_user_opens_a_handle_on(tmp_vault: Vault) -> None:
    profile = create_user(tmp_vault, NAME)

    user = tmp_vault.for_user(profile.id)

    assert user.path == user_directory(tmp_vault, profile.id)
    assert get_user(user, profile.id) == profile


def test_a_new_user_has_a_subjects_folder_but_no_subject_in_it(tmp_vault: Vault) -> None:
    profile = create_user(tmp_vault, NAME)
    user = tmp_vault.for_user(profile.id)

    assert (user.path / SUBJECTS_DIRNAME).is_dir()
    assert subject_slugs(user) == [], "the .gitkeep that keeps the folder is not a subject"
    assert list_subjects(user) == []

    stored = create_subject(user, "Matemáticas II")

    assert stored.slug == "matematicas-ii"
    assert (user.path / SUBJECTS_DIRNAME / stored.slug).is_dir()


def test_the_gitkeep_is_what_makes_the_folder_survive_a_clone(tmp_vault: Vault) -> None:
    profile = create_user(tmp_vault, NAME)
    git(tmp_vault.root, "add", "--all")
    git(tmp_vault.root, "commit", "--quiet", "-m", "a user")

    tracked = git(tmp_vault.root, "ls-files", f"{USERS_DIRNAME}/{profile.id}").splitlines()

    assert f"{USERS_DIRNAME}/{profile.id}/{SUBJECTS_DIRNAME}/{GITKEEP_NAME}" in tracked
    assert f"{USERS_DIRNAME}/{profile.id}/{USER_PROFILE_NAME}" in tracked


def test_the_id_comes_from_the_name_with_its_accents_stripped(tmp_vault: Vault) -> None:
    assert create_user(tmp_vault, "José Ángel Ñandú").id == "jose-angel-nandu"
    assert create_user(tmp_vault, "  Ana   García  ").id == "ana-garcia"


def test_a_second_user_of_a_name_whose_id_is_taken_gets_the_first_free_suffix(
    tmp_vault: Vault,
) -> None:
    first = create_user(tmp_vault, NAME)
    second = create_user(tmp_vault, "Ana Garcia")
    third = create_user(tmp_vault, NAME)

    assert [first.id, second.id, third.id] == ["ana-garcia", "ana-garcia-2", "ana-garcia-3"]
    assert user_ids(tmp_vault) == ["ana-garcia", "ana-garcia-2", "ana-garcia-3"]
    assert get_user(tmp_vault, second.id).name == "Ana Garcia"


def test_a_half_created_user_still_holds_its_id(tmp_vault: Vault) -> None:
    user_directory(tmp_vault, "ana-garcia").mkdir(parents=True)

    assert user_ids(tmp_vault) == ["ana-garcia"], "no profile.json, but the folder is somebody's"
    assert create_user(tmp_vault, NAME).id == "ana-garcia-2"


def test_the_name_is_stored_trimmed(tmp_vault: Vault) -> None:
    assert create_user(tmp_vault, f"  {NAME} \n ").name == NAME


def test_a_user_created_without_an_email_says_it_has_none(tmp_vault: Vault) -> None:
    profile = create_user(tmp_vault, NAME)

    assert profile.email is None
    assert '"email": null' in profile_file(tmp_vault, profile.id).read_text(encoding="utf-8"), (
        "the file itself says what it can hold"
    )


def test_creating_a_user_on_a_user_handle_is_refused(tmp_vault: Vault) -> None:
    add_user(tmp_vault, "ana-garcia", NAME)
    user = tmp_vault.for_user("ana-garcia")

    with pytest.raises(ValueError):
        create_user(user, "Luis Martín")
    assert user_ids(tmp_vault) == ["ana-garcia"], "a user cannot add somebody to the vault"


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "."])
def test_a_name_that_leaves_nothing_to_call_the_user_by_is_refused(
    tmp_vault: Vault, name: str
) -> None:
    with pytest.raises(UserProfileError) as refusal:
        create_user(tmp_vault, name)

    assert refusal.value.args[0], "the message is what the profile screen shows"
    assert not (tmp_vault.root / USERS_DIRNAME).exists(), "a refusal writes nothing"


def test_a_name_longer_than_the_limit_is_refused(tmp_vault: Vault) -> None:
    too_long = "a" * (MAX_USER_NAME_CHARACTERS + 1)

    with pytest.raises(UserProfileError):
        create_user(tmp_vault, too_long)
    assert create_user(tmp_vault, "a" * MAX_USER_NAME_CHARACTERS).name == too_long[:-1]


@pytest.mark.parametrize(
    "email",
    ["ana@", "ana@ejemplo", "ana @ejemplo.com", "ana@@ejemplo.com", "@ejemplo.com", "ana@ejemplo."],
)
def test_an_email_that_is_not_one_is_refused(tmp_vault: Vault, email: str) -> None:
    with pytest.raises(UserProfileError):
        create_user(tmp_vault, NAME, email=email)

    assert not (tmp_vault.root / USERS_DIRNAME).exists()


def test_an_email_longer_than_the_limit_is_refused(tmp_vault: Vault) -> None:
    too_long = f"{'a' * MAX_USER_EMAIL_CHARACTERS}@ejemplo.com"

    with pytest.raises(UserProfileError):
        create_user(tmp_vault, NAME, email=too_long)


def test_an_email_is_stored_trimmed_and_an_empty_one_is_no_email(tmp_vault: Vault) -> None:
    assert create_user(tmp_vault, NAME, email=f"  {EMAIL} ").email == EMAIL
    assert create_user(tmp_vault, "Luis Martín", email="   ").email is None


def test_a_refusal_is_a_value_error_and_a_vault_refusal(tmp_vault: Vault, tmp_path: Path) -> None:
    assert issubclass(UserProfileError, ValueError)
    assert issubclass(UserProfileError, VaultError)
    assert issubclass(UserFileError, UserError)
    before = everything_under(tmp_path)

    with pytest.raises(ValueError):
        create_user(tmp_vault, "")

    assert everything_under(tmp_path) == before


def test_a_name_that_names_no_folder_is_refused_in_spanish(tmp_vault: Vault) -> None:
    with pytest.raises(UserProfileError) as refusal:
        create_user(tmp_vault, "¡¿?")

    assert "letra o un número" in refusal.value.args[0]
    assert not (tmp_vault.root / USERS_DIRNAME).exists()


def test_users_are_listed_by_name_whatever_the_case_and_then_by_id(tmp_vault: Vault) -> None:
    add_user(tmp_vault, "zeta", "Ana")
    add_user(tmp_vault, "alfa", "ana")
    create_user(tmp_vault, "Beatriz")
    create_user(tmp_vault, "Ana García")

    assert [profile.id for profile in list_users(tmp_vault)] == [
        "alfa",
        "zeta",
        "ana-garcia",
        "beatriz",
    ]


def test_the_listing_folds_the_case_but_not_the_accents(tmp_vault: Vault) -> None:
    """Two spellings of one name are one place in the list; an accent is not its letter.

    Names are compared case-insensitively and by nothing else, so an accented vowel sorts by its
    code point, after every plain letter. A collation table would put "Ángel" between "Ana" and
    "Beatriz"; this backend has none, and the selection screen (#552) is free to re-sort what it
    is given.
    """
    create_user(tmp_vault, "Ángel")
    create_user(tmp_vault, "Beatriz")
    create_user(tmp_vault, "Ana")

    assert [profile.id for profile in list_users(tmp_vault)] == ["ana", "beatriz", "angel"]


def test_listing_users_reads_back_every_profile(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)

    assert list_users(tmp_vault) == [created]
    assert list_users(tmp_vault.for_user(created.id)) == [created], "users are the repository's"


def test_a_user_is_read_back_as_it_was_written(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)

    read = get_user(tmp_vault, created.id)

    assert read == created
    assert read.created_at.tzinfo is not None, "the profile says when, and when is an instant"


def test_reading_a_user_that_is_not_there_is_refused(tmp_vault: Vault) -> None:
    with pytest.raises(UserNotFoundError):
        get_user(tmp_vault, "ana-garcia")


def test_reading_a_folder_that_holds_no_profile_is_refused_as_no_such_user(
    tmp_vault: Vault,
) -> None:
    user_directory(tmp_vault, "ana-garcia").mkdir(parents=True)

    with pytest.raises(UserNotFoundError):
        get_user(tmp_vault, "ana-garcia")


@pytest.mark.parametrize(
    "user_id",
    ["", "..", "../ana-garcia", "ana-garcia/../ana-garcia", "Ana García", "ana_garcia", "-ana-"],
)
def test_an_id_that_is_not_a_slug_is_refused_before_the_disk_is_looked_at(
    tmp_vault: Vault, user_id: str
) -> None:
    add_user(tmp_vault, "ana-garcia", NAME)

    with pytest.raises(UserNotFoundError):
        get_user(tmp_vault, user_id)
    with pytest.raises(UserNotFoundError):
        update_user(tmp_vault, user_id, name="Otra")


def test_a_profile_that_is_not_json_is_a_damaged_file_not_a_missing_user(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)
    profile_file(tmp_vault, created.id).write_text("{ no es JSON", encoding="utf-8")

    with pytest.raises(UserFileError):
        get_user(tmp_vault, created.id)
    with pytest.raises(UserFileError):
        list_users(tmp_vault)


@pytest.mark.parametrize(
    "contents",
    [
        {},
        {"id": "ana-garcia", "name": NAME},
        {"id": "ana-garcia", "name": NAME, "created_at": "no es una fecha"},
        {"id": "ana-garcia", "name": NAME, "created_at": 1, "otro_campo": "x"},
        {"id": "ana-garcia", "name": 3, "created_at": 1},
    ],
)
def test_a_profile_that_is_not_a_user_profile_is_refused(tmp_vault: Vault, contents: dict) -> None:
    created = create_user(tmp_vault, NAME)
    profile_file(tmp_vault, created.id).write_text(json.dumps(contents), encoding="utf-8")

    with pytest.raises(UserFileError):
        get_user(tmp_vault, created.id)


def test_user_ids_lists_the_folders_without_reading_a_single_profile(tmp_vault: Vault) -> None:
    create_user(tmp_vault, "Beatriz")
    create_user(tmp_vault, NAME)
    profile_file(tmp_vault, "ana-garcia").write_text("{ roto", encoding="utf-8")

    assert user_ids(tmp_vault) == ["ana-garcia", "beatriz"]


def test_updating_a_name_keeps_the_id_the_photo_and_the_moment_of_creation(
    tmp_vault: Vault,
) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    write_profile(tmp_vault, created.model_copy(update={"photo": "photo.jpg"}))

    updated = update_user(tmp_vault, created.id, name="Ana G.")

    assert (updated.name, updated.id, updated.photo, updated.email) == (
        "Ana G.",
        created.id,
        "photo.jpg",
        EMAIL,
    )
    assert updated.created_at == created.created_at
    assert get_user(tmp_vault, created.id) == updated
    assert user_directory(tmp_vault, created.id).is_dir(), "the folder is not renamed with it"


def test_updating_an_email_keeps_the_name(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME)

    assert update_user(tmp_vault, created.id, email=EMAIL).email == EMAIL
    assert get_user(tmp_vault, created.id).name == NAME


def test_an_empty_email_clears_it_and_a_missing_one_keeps_it(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)

    assert update_user(tmp_vault, created.id, email="   ").email is None
    assert update_user(tmp_vault, created.id, name="Ana G.").email is None
    assert get_user(tmp_vault, created.id).email is None


def test_an_update_of_nothing_leaves_the_profile_as_it_was(tmp_vault: Vault) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    before = profile_file(tmp_vault, created.id).read_bytes()

    assert update_user(tmp_vault, created.id) == created
    assert update_user(tmp_vault, created.id, name=NAME, email=EMAIL) == created
    assert profile_file(tmp_vault, created.id).read_bytes() == before


@pytest.mark.parametrize("field", ["name", "email"])
def test_an_update_that_is_refused_leaves_the_profile_it_found(
    tmp_vault: Vault, field: str
) -> None:
    created = create_user(tmp_vault, NAME, email=EMAIL)
    before = profile_file(tmp_vault, created.id).read_bytes()
    value = "" if field == "name" else "no es un correo"

    with pytest.raises(UserProfileError):
        update_user(tmp_vault, created.id, **{field: value})

    assert profile_file(tmp_vault, created.id).read_bytes() == before
    assert get_user(tmp_vault, created.id) == created


def test_updating_a_user_that_is_not_there_is_refused(tmp_vault: Vault) -> None:
    with pytest.raises(UserNotFoundError):
        update_user(tmp_vault, "ana-garcia", name="Otra")
    # the id is looked up before the fields are validated, so a refusal about the user wins
    with pytest.raises(UserNotFoundError):
        update_user(tmp_vault, "ana-garcia", name="")


def test_a_user_handle_reads_and_edits_the_profiles_of_the_whole_vault(tmp_vault: Vault) -> None:
    ana = create_user(tmp_vault, NAME)
    create_user(tmp_vault, "Luis Martín")
    user = tmp_vault.for_user(ana.id)

    assert user_ids(user) == ["ana-garcia", "luis-martin"]
    assert update_user(user, "luis-martin", email="luis@instituto.es").email == "luis@instituto.es"
    assert get_user(user, ana.id) == ana


def test_the_package_re_exports_the_users_surface() -> None:
    exported = {
        "UserProfile",
        "UserProfileError",
        "UserFileError",
        "UserNotFoundError",
        "UserError",
        "create_user",
        "get_user",
        "list_users",
        "update_user",
        "user_ids",
    }

    assert exported <= set(vault_package.__all__)
    assert all(hasattr(vault_package, name) for name in exported)
