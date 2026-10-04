"""The derived index as one user sees it: one database per user, and what each of them indexes.

An index is built over the handle it is given, so a search answers with one student's own content
in the ids that student's callers use and never with another's, however alike the two folders are.
What belongs to the repository rather than to one user is read there: the HEAD a rebuild is keyed
to, and the notes tags, which carry the user's id. And a root handle's rebuild is one rebuild per
user, in `list_users` order, each in a database of its own beside the configured path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import ManualClock, git
from user_helpers import add_user

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    GitSync,
    UserNotFoundError,
    Vault,
    create_subject,
    create_topic,
    topic_directory,
    write_notes,
)
from studentassistant.vault.index import (
    DOC_NOTES,
    VaultIndex,
    rebuild_index,
    user_index_path,
)

SUBJECT = "fisica"
TOPIC = "cinematica"
# The notes id as a user's own handle gives it: relative to their folder, not to the repository.
NOTES = f"subjects/{SUBJECT}/topics/{TOPIC}/notes/apuntes.md"
# In both users' notes on purpose: the same word in both is what a search must not cross.
WORD = "fotosíntesis"


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "index.sqlite3"


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    """The repository's one sync, on a clock a test moves: it tags without ever pushing."""
    return GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())


def give_notes(vault: Vault, user_id: str, name: str, organ: str) -> Vault:
    """A user with one physics topic whose notes hold `WORD` and one word no other user's has."""
    user = add_user(vault, user_id, name)
    create_subject(user, "Física")
    create_topic(user, SUBJECT, "Cinemática")
    write_notes(user, SUBJECT, TOPIC, f"# {name}\nLa {WORD} ocurre en {organ}.\n")
    return user


def test_a_search_over_one_users_index_never_answers_with_another_users_notes(
    tmp_vault: Vault, index_path: Path
) -> None:
    ana = give_notes(tmp_vault, "ana", "Ana García", "los cloroplastos")
    luis = give_notes(tmp_vault, "luis", "Luis Martín", "las mitocondrias")

    with VaultIndex.open(ana, user_index_path(index_path, "ana")) as index:
        [hit] = index.search("fotosintesis")
        assert (hit.kind, hit.path, hit.subject, hit.topic) == (DOC_NOTES, NOTES, SUBJECT, TOPIC)
        assert "cloroplastos" in hit.snippet
        # Luis' notes are in the same repository, and in no row of this database.
        assert index.search("mitocondrias") == []
        assert [(s.slug, s.name) for s in index.subjects()] == [(SUBJECT, "Física")]
        assert [t.slug for t in index.topics(SUBJECT)] == [TOPIC]

    with VaultIndex.open(luis, user_index_path(index_path, "luis")) as index:
        [hit] = index.search("fotosintesis")
        assert hit.path == NOTES
        assert "mitocondrias" in hit.snippet
        assert index.search("cloroplastos") == []

    assert user_index_path(index_path, "ana").is_file()
    assert user_index_path(index_path, "luis").is_file()


def test_user_index_path_names_a_database_beside_the_configured_one() -> None:
    assert user_index_path(Path("/home/ana/.cache/studentassistant/index.sqlite3"), "ana") == Path(
        "/home/ana/.cache/studentassistant/index-ana.sqlite3"
    )
    assert user_index_path(Path("cache/index"), "luis-2") == Path("cache/index-luis-2")
    assert user_index_path(Path("index.sqlite3"), "ana") == Path("index-ana.sqlite3")


@pytest.mark.parametrize("user_id", ["../fuera", "Ana García", "", "ana/luis", " ana"])
def test_user_index_path_refuses_an_id_that_is_not_a_user_id(user_id: str) -> None:
    with pytest.raises(UserNotFoundError):
        user_index_path(Path("cache/index.sqlite3"), user_id)


def test_a_users_index_lists_only_their_own_notes_versions(
    tmp_vault: Vault, sync: GitSync, index_path: Path
) -> None:
    ana = give_notes(tmp_vault, "ana", "Ana García", "los cloroplastos")
    luis = give_notes(tmp_vault, "luis", "Luis Martín", "las mitocondrias")
    of_ana, of_luis = sync.for_user("ana"), sync.for_user("luis")
    of_ana.note_change()
    first = of_ana.create_notes_tag(SUBJECT, TOPIC)
    write_notes(ana, SUBJECT, TOPIC, f"# Ana v2\nLa {WORD}, revisada.\n")
    of_ana.note_change()
    second = of_ana.create_notes_tag(SUBJECT, TOPIC)
    of_luis.note_change()
    of_luis.create_notes_tag(SUBJECT, TOPIC)
    # A tag of the repository's own content, in the form it had before there were user folders.
    sync.create_notes_tag(SUBJECT, TOPIC)

    with VaultIndex.open(ana, user_index_path(index_path, "ana")) as index:
        versions = index.note_versions(SUBJECT, TOPIC)
        # Two users with the same two slugs keep two version sequences, each starting at one.
        assert [(v.version, v.name) for v in versions] == [
            (1, f"ana/{SUBJECT}/{TOPIC}/apuntes-v1"),
            (2, f"ana/{SUBJECT}/{TOPIC}/apuntes-v2"),
        ]
        assert [v.commit for v in versions] == [first.commit, second.commit]
        assert first.name == f"ana/{SUBJECT}/{TOPIC}/apuntes-v1"

    with VaultIndex.open(luis, user_index_path(index_path, "luis")) as index:
        assert [v.name for v in index.note_versions()] == [f"luis/{SUBJECT}/{TOPIC}/apuntes-v1"]

    # The repository's own index reads the tags that name no user, and none of theirs.
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert [v.name for v in index.note_versions()] == [f"{SUBJECT}/{TOPIC}/apuntes-v1"]
        # Nor does it read a user's content: what is under `users/` is nobody's own at the root.
        assert index.subjects() == []
        assert index.search("fotosintesis") == []


def test_a_users_index_is_keyed_to_the_repositorys_head(tmp_vault: Vault, index_path: Path) -> None:
    ana = give_notes(tmp_vault, "ana", "Ana García", "los cloroplastos")
    give_notes(tmp_vault, "luis", "Luis Martín", "las mitocondrias")

    with VaultIndex.open(ana, user_index_path(index_path, "ana")) as index:
        assert index.is_current()

        # A commit of the other user's folder moves the repository's HEAD, which is what this index
        # is keyed to: the pull that carried it may have carried this user's notes tags with it.
        git(tmp_vault.root, "add", "-A")
        git(tmp_vault.root, "commit", "-q", "-m", "los apuntes de Luis")

        assert not index.is_current()
        assert index.refresh().rebuilt
        assert index.is_current()
        assert [hit.path for hit in index.search("fotosintesis")] == [NOTES]


def test_a_root_handle_rebuilds_one_database_per_user_in_list_users_order(
    vault_with_no_users: Vault, index_path: Path
) -> None:
    # `list_users` sorts by name, so these two come out in the other order than their ids do. The
    # vault has nobody else in it: the user `Vault.init` creates is named "Ana García" too, and a
    # third user of that name would say nothing about the order (#548).
    ana = give_notes(vault_with_no_users, "ana", "Zoe Ruiz", "los cloroplastos")
    bea = give_notes(vault_with_no_users, "bea", "Ana García", "las mitocondrias")

    report = rebuild_index(vault_with_no_users, index_path)

    assert [user_id for user_id, _ in report.users] == ["bea", "ana"]
    assert report.rebuilt
    assert report.documents == sum(one.documents for _, one in report.users) > 0
    assert report.units_indexed == sum(one.units_indexed for _, one in report.users)
    assert all(one.users == () for _, one in report.users)
    assert user_index_path(index_path, "ana").is_file()
    assert user_index_path(index_path, "bea").is_file()
    # Nothing at the root is anybody's content, so no database is written at the path itself.
    assert not index_path.exists()

    # Each of those databases is the one that user's handle opens, and answers for them alone.
    with VaultIndex.open(ana, user_index_path(index_path, "ana")) as index:
        assert index.refresh().rebuilt is False
        assert [hit.path for hit in index.search("fotosintesis")] == [NOTES]
        assert index.search("mitocondrias") == []
    with VaultIndex.open(bea, user_index_path(index_path, "bea")) as index:
        assert index.refresh().rebuilt is False
        assert index.search("cloroplastos") == []


def test_a_user_handle_rebuilds_only_the_one_database_it_is_given(
    tmp_vault: Vault, index_path: Path
) -> None:
    ana = give_notes(tmp_vault, "ana", "Ana García", "los cloroplastos")
    give_notes(tmp_vault, "luis", "Luis Martín", "las mitocondrias")

    report = rebuild_index(ana, user_index_path(index_path, "ana"))

    assert report.users == ()
    assert report.documents == 1
    assert user_index_path(index_path, "ana").is_file()
    assert not user_index_path(index_path, "luis").exists()


def test_a_unit_a_users_index_cannot_read_is_named_as_theirs_in_the_combined_report(
    tmp_vault: Vault, index_path: Path
) -> None:
    ana = give_notes(tmp_vault, "ana", "Ana García", "los cloroplastos")
    give_notes(tmp_vault, "luis", "Luis Martín", "las mitocondrias")
    broken = topic_directory(ana, SUBJECT, TOPIC) / "topic.yaml"
    broken.write_text("not: [a, topic\n", encoding="utf-8")

    report = rebuild_index(tmp_vault, index_path)

    # Two users' units are the same names, so the combined report says whose it was; each user's
    # own report keeps the name their callers know.
    assert [unit for unit, _ in report.skipped] == [f"ana/topic/{SUBJECT}/{TOPIC}"]
    by_user = dict(report.users)
    assert [unit for unit, _ in by_user["ana"].skipped] == [f"topic/{SUBJECT}/{TOPIC}"]
    assert by_user["luis"].skipped == ()
    assert by_user["luis"].documents == 1


def test_a_vault_with_no_users_keeps_the_one_database_at_the_path(
    vault_with_no_users: Vault, index_path: Path
) -> None:
    create_subject(vault_with_no_users, "Física")
    create_topic(vault_with_no_users, SUBJECT, "Cinemática")
    write_notes(
        vault_with_no_users, SUBJECT, TOPIC, f"# de siempre\nLa {WORD} de antes de los usuarios.\n"
    )

    report = rebuild_index(vault_with_no_users, index_path)

    assert report.users == ()
    assert report.documents == 1
    assert index_path.is_file()
    with VaultIndex.open(vault_with_no_users, index_path) as index:
        assert [hit.path for hit in index.search("fotosintesis")] == [NOTES]
        assert index.note_versions() == []
