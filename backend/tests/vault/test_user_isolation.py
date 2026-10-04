"""Writer isolation on a user handle: what the writers touch stays in that user's own folder, and
the vault-relative ids they hand back are relative to it, not to the repository."""

from __future__ import annotations

import pytest
from user_helpers import (
    GENERATED,
    GENERATED_TEXT,
    add_user,
    everything_under,
    handle_relative,
    write_everything,
)

from studentassistant.vault import (
    Vault,
    list_generated,
    list_sources,
    list_subjects,
    read_generated,
    read_notes,
    read_source,
)
from studentassistant.vault.locking import LOCKS_DIRNAME

# Both users below get these same two slugs on purpose: the point is that the same id names a
# different file for each of them. They are the slugs `write_everything`'s default names give.
SLUGS = ("matematicas-ii", "derivadas")


@pytest.fixture
def ana(tmp_vault: Vault) -> Vault:
    return add_user(tmp_vault, "ana-garcia", "Ana García")


@pytest.fixture
def luis(tmp_vault: Vault) -> Vault:
    return add_user(tmp_vault, "luis-martin", "Luis Martín")


def test_every_writer_of_a_user_handle_writes_inside_that_users_folder(
    tmp_vault: Vault, ana: Vault
) -> None:
    before = set(everything_under(tmp_vault.root))

    paths = write_everything(ana, "las derivadas de Ana")

    added = set(everything_under(tmp_vault.root)) - before
    assert added, "the writers wrote nothing at all, so this test would pass on its own"
    # `.git/` is the repository's own and no content: the locks a writer takes live in it, and
    # they are the repository's whatever handle asked for them, so they are not "outside" (#547).
    outside = sorted(
        path
        for path in added
        if path.parts[:2] != ("users", "ana-garcia") and path.parts[0] != ".git"
    )
    assert outside == [], "a writer given a user handle put something outside users/ana-garcia/"
    taken = sorted(path for path in added if path.parts[0] == ".git")
    assert taken, "no writer took a lock, so this test would pass on its own"
    assert all(path.parts[1] == LOCKS_DIRNAME for path in taken), (
        "the locks of a user handle's writers are the repository's, under its own .git/"
    )
    assert all(path.is_relative_to(ana.path) for path in paths)
    assert handle_relative(ana, paths[0]) == (
        "subjects/matematicas-ii/topics/derivadas/sources/notes/page-001.jpg"
    ), "the path a writer returns is the one relative to the user's folder"


def test_the_ids_a_user_handle_hands_back_are_relative_to_its_folder_and_read_back(
    ana: Vault,
) -> None:
    write_everything(ana, "las derivadas de Ana")

    sources = list_sources(ana, *SLUGS)
    assert sources, "no source was stored, so this test would pass on its own"
    for stored in sources:
        assert stored.path.startswith("subjects/matematicas-ii/topics/derivadas/sources/")
        assert read_source(ana, stored.path).content, f"{stored.path} is not readable back"
    assert list_generated(ana, *SLUGS) == [
        f"subjects/{SLUGS[0]}/topics/{SLUGS[1]}/generated/{GENERATED}"
    ], "generated material is listed by the same kind of id, relative to the user's folder"
    assert read_generated(ana, *SLUGS, GENERATED) == GENERATED_TEXT.encode("utf-8")


def test_two_users_with_the_same_slugs_never_see_each_others_files(
    tmp_vault: Vault, ana: Vault, luis: Vault
) -> None:
    write_everything(ana, "las derivadas de Ana")
    write_everything(luis, "las derivadas de Luis")

    assert [stored.slug for stored in list_subjects(ana)] == [SLUGS[0]]
    assert [stored.slug for stored in list_subjects(luis)] == [SLUGS[0]]
    assert read_notes(ana, *SLUGS) == "# las derivadas de Ana\n"
    assert read_notes(luis, *SLUGS) == "# las derivadas de Luis\n"

    ana_sources = [stored.path for stored in list_sources(ana, *SLUGS)]
    luis_sources = [stored.path for stored in list_sources(luis, *SLUGS)]
    assert ana_sources == luis_sources, "the same content gives the same ids to both users"
    web = next(path for path in ana_sources if "/sources/web/" in path)
    assert read_source(ana, web).content != read_source(luis, web).content, (
        "one id, two files: each user reads its own"
    )

    assert list_subjects(tmp_vault) == [], "the root handle has no content of its own"
    assert not (tmp_vault.root / "subjects").exists()
