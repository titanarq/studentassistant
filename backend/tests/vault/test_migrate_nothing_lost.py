"""What the move of a whole vault costs the student whose vault it is: nothing (#548).

`test_migrate.py` is about what one run does and what it refuses to do. This one is about the only
question a student whose vault holds everything they wrote actually has, and the one the migration
exists to answer: after `migrate_to_users` has moved the root's content into the first user's
folder, is all of it still there, still the same, still readable and still findable?

So the vault here is built with every writer family the vault has -- two subjects, each with a
session of events and a transcript, a source of every kind, notes in two tagged versions,
generated material, state, conversations, ledger and study records -- and then four things are
compared across the move, one per test: the bytes of every file, the history git can follow
through the move, the notes versions and the text each of them reads, and the search hits the
derived index answers with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import ManualClock, git
from user_helpers import write_everything

from studentassistant.config import VaultGitSettings
from studentassistant.vault import GitSync, Vault, list_sources, list_users, write_notes
from studentassistant.vault.index import VaultIndex, rebuild_index, user_index_path
from studentassistant.vault.migrate import MigrationReport, migrate_to_users
from studentassistant.vault.slugs import slugify
from studentassistant.vault.sources import SOURCE_KINDS
from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.users import GITKEEP_NAME
from studentassistant.vault.vault import USERS_DIRNAME

# `legacy_vault`'s `vault.yaml` says "Ana García", so this is the id the first user is created with.
FIRST_USER = "ana-garcia"
# Two subjects rather than one: "the same number of notes versions per topic" says something only
# over more than one topic, and an index with two subjects to answer from is the one a student has.
# Each entry is the subject's name, its topic's, and what that topic's content says.
TOPICS = (
    ("Física", "Cinemática", "velocidad media"),
    ("Matemáticas II", "Derivadas", "derivada de una función"),
)
SLUGS = tuple((slugify(subject), slugify(topic)) for subject, topic, _ in TOPICS)
# Words of that content, chosen so that between them they reach more than one kind of document the
# index indexes -- the notes, a page's transcription, a transcript, a PDF's derived text -- plus one
# nobody ever wrote: what a search found before the move it must find, field for field, after it,
# and what it did not find must still not be there.
QUERIES = ("velocidad", "derivada", "función", "aceleración", "cuántico")


@pytest.fixture
def sync(legacy_vault: Vault) -> GitSync:
    """The repository's own sync, on a clock a test moves; no remote, so nothing is pushed."""
    return GitSync(legacy_vault, VaultGitSettings(), clock=ManualClock())


@pytest.fixture
def vault(legacy_vault: Vault, sync: GitSync) -> Vault:
    """A format-1 vault holding everything the public writers can put in one, twice over.

    Each of the two topics gets a session, a source of every kind, notes, generated material,
    state, conversations, ledger and study records from `write_everything`, and then a second
    version of its notes, so that every topic has two tagged versions for the move to keep.
    """
    for (subject, topic, text), (subject_slug, topic_slug) in zip(TOPICS, SLUGS, strict=True):
        write_everything(legacy_vault, text, subject=subject, topic=topic)
        tag_notes(sync, legacy_vault, subject_slug, topic_slug, f"# {text}\n", 1)
        tag_notes(
            sync,
            legacy_vault,
            subject_slug,
            topic_slug,
            f"# {text}\n\n## Segunda versión\n",
            2,
        )
    return legacy_vault


def tag_notes(
    sync: GitSync, vault: Vault, subject: str, topic: str, notes: str, version: int
) -> None:
    """Commit `notes` as this topic's notes and tag it the version `version` of them."""
    write_notes(vault, subject, topic, notes)
    sync.checkpoint(f"apuntes v{version} de {subject}/{topic}")
    sync.create_notes_tag(subject, topic, f"Apuntes v{version} de {subject}/{topic}")


def at_root(relative: str) -> str:
    """A path inside the root's `subjects/` as the repository of format 1 names it."""
    return f"{SUBJECTS_DIRNAME}/{relative}"


def under_user(repository_relative: str) -> str:
    """A path the repository of format 1 names, as the one of format 2 names it.

    `repository_relative` is one a handle or `report.moved_files` gives (`subjects/fisica/...`),
    which is not what `files_under` keys its bytes by -- see `at_root`.
    """
    return f"{USERS_DIRNAME}/{FIRST_USER}/{repository_relative}"


def notes_of(subject: str, topic: str) -> str:
    """The notes' id as a handle gives it, which is the same string before the move and after it."""
    return f"subjects/{subject}/topics/{topic}/notes/apuntes.md"


def files_under(directory: Path) -> dict[str, bytes]:
    """Every file below `directory`, by its path relative to it, with its bytes."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def hits(index: VaultIndex, query: str) -> list[tuple[object, ...]]:
    """A search's whole answer, ordered: all nine fields of every hit it gives.

    Ordered by a key that cannot trip over the fields a notes hit leaves `None` and a transcript
    one fills in, so that the two answers this compares are comparable at all.
    """
    return [
        (
            hit.kind,
            hit.path,
            hit.source,
            hit.subject,
            hit.topic,
            hit.session,
            hit.seq,
            hit.t_start,
            hit.snippet,
        )
        for hit in sorted(index.search(query), key=lambda hit: (hit.kind, hit.path, hit.seq or 0))
    ]


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "index.sqlite3"


def test_every_file_the_root_held_is_byte_identical_under_the_user(
    vault: Vault, sync: GitSync
) -> None:
    before = files_under(vault.root / SUBJECTS_DIRNAME)
    assert len(before) > 20, "a vault with everything in it, or this test would prove nothing"
    sources = list_sources(vault, *SLUGS[0])
    kinds = {source.kind for source in sources}
    assert kinds == set(SOURCE_KINDS), "a source of every kind there is, among it all"
    ids = [source.path for source in sources]

    report = migrate_to_users(vault, sync)

    after = files_under(vault.root / USERS_DIRNAME / FIRST_USER / SUBJECTS_DIRNAME)
    assert after.pop(GITKEEP_NAME, None) == b"", (
        "the one file the user's subjects/ has that the root's had not is the .gitkeep a user is"
        " created with"
    )
    assert after == before, "every file, at the same path inside subjects/, with the same bytes"
    assert set(report.moved_files) == {at_root(relative) for relative in before}, (
        "and the report named each of them by the path it had"
    )
    assert report.left_behind == ()
    assert not (vault.root / SUBJECTS_DIRNAME).exists(), "the root keeps no content of anybody's"

    migrated = Vault.open(vault.root)
    assert [profile.id for profile in list_users(migrated)] == [FIRST_USER]
    moved = list_sources(migrated.for_user(FIRST_USER), *SLUGS[0])
    assert {source.kind for source in moved} == kinds
    assert [source.path for source in moved] == ids, (
        "and each source is still the id its handle always gave it"
    )


def test_the_history_of_a_moved_notes_file_reaches_the_commits_it_had(
    vault: Vault, sync: GitSync
) -> None:
    notes = notes_of(*SLUGS[0])
    history = set(git(vault.root, "log", "--format=%H", "--", notes).split())
    assert len(history) >= 2, "two versions of the notes are two commits to find again"

    report = migrate_to_users(vault, sync)

    followed = set(
        git(vault.root, "log", "--follow", "--format=%H", "--", under_user(notes)).split()
    )
    assert history <= followed, "every commit the notes had before the move is still one of its own"
    assert report.commit in followed, "and so is the commit that moved them"


def test_every_notes_version_keeps_its_number_its_message_and_its_text(
    vault: Vault, sync: GitSync
) -> None:
    before = {
        slugs: [
            (tag.name, tag.version, tag.message, sync.read_file_at(tag.name, notes_of(*slugs)))
            for tag in sync.list_notes_tags(*slugs)
        ]
        for slugs in SLUGS
    }
    assert [len(versions) for versions in before.values()] == [2, 2], "two versions per topic"
    assert all(text for versions in before.values() for *_, text in versions), "each readable now"

    report = migrate_to_users(vault, sync)
    view = sync.for_user(FIRST_USER)

    for slugs, versions in before.items():
        after = view.list_notes_tags(*slugs)
        assert len(after) == len(versions), "the same number of notes versions per topic"
        assert [(tag.version, tag.message) for tag in after] == [
            (version, message) for _, version, message, _ in versions
        ], "each one keeping the number and the message it was given"
        assert {tag.commit for tag in after} == {report.commit}

        # A tag of before the move names a commit of before it, where the notes were at the root:
        # `legacy_root_user` is what lets this user's view read them there (#547).
        notes = notes_of(*slugs)
        for name, _, _, text in versions:
            assert view.read_file_at(name, notes) == text, f"{name} still reads what it read"
        assert [tag.name for tag in sync.list_notes_tags(*slugs)] == [
            name for name, _, _, _ in versions
        ], "and the repository keeps the tags it had, on the commits it had them on"


def test_the_users_index_finds_the_search_hits_the_roots_index_found(
    vault: Vault, sync: GitSync, index_path: Path
) -> None:
    rebuild_index(vault, index_path)
    with VaultIndex.open(vault, index_path) as index:
        before = {query: hits(index, query) for query in QUERIES}
    answered = [query for query, found in before.items() if found]
    assert len(answered) >= 3, f"the content the vault holds is findable by {answered}"
    assert before[QUERIES[-1]] == [], "and a word nobody ever wrote finds nothing, as it must"
    kinds = {found[0] for results in before.values() for found in results}
    assert len(kinds) >= 3, f"and in more than one kind of document: {sorted(kinds)}"

    migrate_to_users(vault, sync)

    migrated = Vault.open(vault.root)
    report = rebuild_index(migrated, index_path)
    assert [user_id for user_id, _ in report.users] == [FIRST_USER]
    database = user_index_path(index_path, FIRST_USER)
    with VaultIndex.open(migrated.for_user(FIRST_USER), database) as index:
        after = {query: hits(index, query) for query in QUERIES}

    assert after == before, "the same hits, in the same ids, out of the user's own database"
    assert database.is_file()


def test_a_dry_run_of_a_vault_this_full_changes_none_of_it(vault: Vault, sync: GitSync) -> None:
    subjects = files_under(vault.root / SUBJECTS_DIRNAME)
    tags = git(vault.root, "tag", "--list").split()
    commits = git(vault.root, "rev-list", "--count", "HEAD").strip()

    report: MigrationReport = migrate_to_users(vault, sync, dry_run=True)

    assert files_under(vault.root / SUBJECTS_DIRNAME) == subjects
    assert git(vault.root, "tag", "--list").split() == tags
    assert git(vault.root, "rev-list", "--count", "HEAD").strip() == commits
    assert not (vault.root / USERS_DIRNAME).exists()
    assert not report.migrated and report.commit is None
    assert set(report.moved_files) == {at_root(relative) for relative in subjects}, (
        "and it said what the run that changes it would move"
    )
