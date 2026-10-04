"""`vault migrate-users`: the move of a format-1 vault into its first user's folder (#548).

A migration is the whole repository's, so these tests are about what one run does to a vault and
what it refuses to do to one: the move and the one commit that carries it, the notes versions it
re-names under the first user, the dry run that changes nothing, and the states a vault must not be
in for it to happen at all. What the move costs a student whose vault holds everything -- every
file byte for byte, the history of their notes, their versions and their searches -- is
`test_migrate_nothing_lost.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from git_helpers import ManualClock, commit_count, git
from user_helpers import everything_under

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    end_session,
    list_users,
    start_session,
    write_notes,
)
from studentassistant.vault.migrate import (
    MIGRATION_COMMIT_PREFIX,
    MigrationError,
    MigrationRefusedError,
    migrate_to_users,
    migration_commit_subject,
)
from studentassistant.vault.models import FORMAT_VERSION, LEGACY_FORMAT_VERSION
from studentassistant.vault.sync import GitSync
from studentassistant.vault.users import UserProfileError, get_user
from studentassistant.vault.vault import MIGRATE_USERS_COMMAND

# `legacy_vault`'s `vault.yaml` says this, so this is the name the first user is created with.
STUDENT = "Ana García"
FIRST_USER = "ana-garcia"
SUBJECT = "Física"
TOPIC = "Cinemática"
SLUGS = ("fisica", "cinematica")
# The notes' id as a handle gives it: relative to the folder that handle points at, so it is the
# same string before the move (the root's) and after it (the user's).
NOTES = f"subjects/{SLUGS[0]}/topics/{SLUGS[1]}/notes/apuntes.md"
NOTES_V1 = "# Cinemática\n"
NOTES_V2 = "# Cinemática\n\n## Movimiento rectilíneo\n"
TAG_V1 = f"{SLUGS[0]}/{SLUGS[1]}/apuntes-v1"
TAG_V2 = f"{SLUGS[0]}/{SLUGS[1]}/apuntes-v2"
MESSAGE_V1 = "Apuntes v1 de fisica/cinematica: Cinemática"
MESSAGE_V2 = "Apuntes v2 de fisica/cinematica: Cinemática, movimiento rectilíneo"


@pytest.fixture
def sync(legacy_vault: Vault) -> GitSync:
    """The repository's own sync, on a clock a test moves; no remote, so nothing is pushed."""
    return GitSync(legacy_vault, VaultGitSettings(), clock=ManualClock())


@pytest.fixture
def vault(legacy_vault: Vault, sync: GitSync) -> Vault:
    """The format-1 vault with one topic at its root, committed, with two notes versions tagged."""
    subject = create_subject(legacy_vault, SUBJECT).slug
    topic = create_topic(legacy_vault, subject, TOPIC).slug
    session = start_session(legacy_vault, subject, topic, host="pc", protocol_version="1.0")
    session.append_event("session_started", "phone")
    session.append_transcript(0, 900, "la velocidad media")
    end_session(session)
    write_notes(legacy_vault, subject, topic, NOTES_V1)
    sync.checkpoint("apuntes de cinemática")
    sync.create_notes_tag(subject, topic, MESSAGE_V1)
    write_notes(legacy_vault, subject, topic, NOTES_V2)
    sync.checkpoint("segunda versión de los apuntes")
    sync.create_notes_tag(subject, topic, MESSAGE_V2)
    return legacy_vault


def content_of(root: Path) -> list[Path]:
    """Every path of the vault but the repository's own `.git/`, where its locks live."""
    return [path for path in everything_under(root) if path.parts[0] != ".git"]


def moved_of(relative: str) -> str:
    """Where the migration puts a repository-relative path of the root's content."""
    return f"users/{FIRST_USER}/{relative}"


def test_the_roots_content_becomes_the_first_users_and_the_vault_says_format_2(
    vault: Vault, sync: GitSync
) -> None:
    report = migrate_to_users(vault, sync)

    assert report.migrated and not report.dry_run
    assert (report.user_id, report.user_name) == (FIRST_USER, STUDENT)
    assert report.subjects == (SLUGS[0],)
    assert NOTES in report.moved_files, "the report names the files as the root held them"
    assert report.left_behind == ()
    assert not (vault.root / "subjects").exists(), "the root keeps no content of anybody's"

    migrated = Vault.open(vault.root)
    assert migrated.meta.format_version == FORMAT_VERSION
    assert migrated.meta.legacy_root_user == FIRST_USER
    assert migrated.meta.student == STUDENT
    assert get_user(migrated, FIRST_USER).name == STUDENT
    assert (migrated.for_user(FIRST_USER).path / NOTES).read_text(encoding="utf-8") == NOTES_V2


def test_the_whole_migration_is_one_commit_named_after_the_user(
    vault: Vault, sync: GitSync
) -> None:
    before = commit_count(vault.root)

    report = migrate_to_users(vault, sync)

    assert commit_count(vault.root) == before + 1, "the move, the profile and vault.yaml are one"
    subject = git(vault.root, "log", "-1", "--format=%s").strip()
    assert subject == migration_commit_subject(FIRST_USER)
    assert subject.startswith(MIGRATION_COMMIT_PREFIX)
    assert report.commit == git(vault.root, "rev-parse", "HEAD").strip()


def test_the_move_is_a_rename_that_gits_history_follows(vault: Vault, sync: GitSync) -> None:
    history_before = set(git(vault.root, "log", "--format=%H", "--", NOTES).split())
    assert history_before, "the notes had no history to follow, so this test would pass on its own"

    report = migrate_to_users(vault, sync)

    moved = f"users/{FIRST_USER}/{NOTES}"
    followed = set(git(vault.root, "log", "--follow", "--format=%H", "--", moved).split())
    assert history_before <= followed, "the commits of the notes before the move are still its own"
    assert report.commit in followed

    # The commit also adds the profile and the user's `.gitkeep` and rewrites `vault.yaml`, so a
    # rename is not its first line: what proves the move is that every file it took is one, and
    # that git recorded neither a copy nor a delete anywhere in it.
    name_status = git(vault.root, "show", "--name-status", "--format=", report.commit)
    statuses = [line.split("\t") for line in name_status.splitlines()]
    renamed = {fields[1]: fields[2] for fields in statuses if fields[0].startswith("R")}
    assert set(renamed) == set(report.moved_files), "every file the move took, git renames"
    assert all(target == moved_of(source) for source, target in renamed.items()), (
        "and renames it into the user's folder, keeping the path it had"
    )
    assert not [fields for fields in statuses if fields[0][0] in "CD"], (
        "git records the move as the rename it is, not as a copy and a delete"
    )


def test_every_notes_version_is_renamed_under_the_user_keeping_its_number_and_message(
    vault: Vault, sync: GitSync
) -> None:
    before = sync.list_notes_tags(*SLUGS)
    assert [tag.name for tag in before] == [TAG_V1, TAG_V2]

    report = migrate_to_users(vault, sync)

    after = sync.for_user(FIRST_USER).list_notes_tags(*SLUGS)
    assert [tag.name for tag in after] == [f"{FIRST_USER}/{TAG_V1}", f"{FIRST_USER}/{TAG_V2}"]
    assert [tag.version for tag in after] == [1, 2], "a version keeps the number it was given"
    assert [tag.message for tag in after] == [MESSAGE_V1, MESSAGE_V2]
    assert {tag.commit for tag in after} == {report.commit}, "the new tags are on the migration"
    kind = git(vault.root, "cat-file", "-t", after[-1].name).strip()
    assert kind == "tag", "annotated, which is what makes a push carry them along with the branch"
    assert sync.list_notes_tags(*SLUGS) == before, "the tags the vault had are kept as they were"
    assert [(tag.old_name, tag.new_name, tag.version) for tag in report.tags] == [
        (TAG_V1, f"{FIRST_USER}/{TAG_V1}", 1),
        (TAG_V2, f"{FIRST_USER}/{TAG_V2}", 2),
    ]
    assert all(tag.created and tag.commit == report.commit for tag in report.tags)


def test_a_version_tagged_before_the_move_still_reads_through_the_users_handle(
    vault: Vault, sync: GitSync
) -> None:
    migrate_to_users(vault, sync)
    view = sync.for_user(FIRST_USER)

    # The tags of before the move name the commits of before it, where the notes were at the root:
    # `legacy_root_user` is what lets this user's handle read them there (`vault/sync.py`, #547).
    assert view.read_file_at(TAG_V1, NOTES) == NOTES_V1
    assert view.read_file_at(TAG_V2, NOTES) == NOTES_V2


def test_a_dry_run_says_what_would_happen_and_changes_nothing(vault: Vault, sync: GitSync) -> None:
    before = content_of(vault.root)
    commits = commit_count(vault.root)
    tags = git(vault.root, "tag", "--list").split()

    report = migrate_to_users(vault, sync, dry_run=True)

    assert report.dry_run and not report.migrated and report.reason is None
    assert (report.user_id, report.user_name) == (FIRST_USER, STUDENT)
    assert report.subjects == (SLUGS[0],)
    assert NOTES in report.moved_files
    assert [(tag.old_name, tag.new_name, tag.created, tag.commit) for tag in report.tags] == [
        (TAG_V1, f"{FIRST_USER}/{TAG_V1}", False, ""),
        (TAG_V2, f"{FIRST_USER}/{TAG_V2}", False, ""),
    ]
    assert report.commit is None and report.pushed is None

    assert content_of(vault.root) == before, "a dry run writes nothing, not even a user"
    assert commit_count(vault.root) == commits, "a dry run does not sync either, which commits"
    assert git(vault.root, "tag", "--list").split() == tags
    assert Vault.open_for_migration(vault.root).meta.format_version == LEGACY_FORMAT_VERSION


def test_an_already_migrated_vault_has_nothing_to_do(vault: Vault, sync: GitSync) -> None:
    migrate_to_users(vault, sync)
    migrated = Vault.open(vault.root)
    commits = commit_count(vault.root)

    again = migrate_to_users(migrated, GitSync(migrated, VaultGitSettings(), clock=ManualClock()))
    planned = migrate_to_users(
        migrated, GitSync(migrated, VaultGitSettings(), clock=ManualClock()), dry_run=True
    )

    for report in (again, planned):
        assert not report.migrated and report.user_id is None
        assert report.reason is not None and f"formato {FORMAT_VERSION}" in report.reason
        assert "nada que migrar" in report.reason
    assert commit_count(vault.root) == commits


def test_an_unended_session_refuses_the_migration_and_names_it(vault: Vault, sync: GitSync) -> None:
    open_session = start_session(vault, *SLUGS, host="pc", protocol_version="1.0")
    commits, before = commit_count(vault.root), content_of(vault.root)

    for dry_run in (False, True):
        with pytest.raises(MigrationRefusedError) as refused:
            migrate_to_users(vault, sync, dry_run=dry_run)
        message = str(refused.value)
        assert f"{SLUGS[0]}/{SLUGS[1]}/{open_session.id}" in message, "the session is named"
        assert MIGRATE_USERS_COMMAND in message, "and so is what to run once it has ended"

    # A run that is not a dry one starts with the sync the migration is specified to start with,
    # and that sync commits what the open session had written so far: the vault's own hygiene,
    # which loses nothing, and not a step of a migration that never happened.
    assert commit_count(vault.root) == commits + 1, "only what was pending, which the sync commits"
    assert git(vault.root, "log", "-1", "--format=%s").strip() != migration_commit_subject(
        FIRST_USER
    ), "and none of them the migration's"
    assert content_of(vault.root) == before
    assert not (vault.root / "users").exists(), "the content stayed where format 1 keeps it"
    assert Vault.open_for_migration(vault.root).meta.format_version == LEGACY_FORMAT_VERSION


def test_a_conflict_with_the_remote_refuses_the_migration_and_names_the_paths(
    vault: Vault, sync: GitSync, git_origin: Path, tmp_path: Path
) -> None:
    sync.push_now()
    other = tmp_path / "otro-ordenador"
    git(tmp_path, "clone", "--quiet", str(git_origin), str(other))
    disputed = vault.root / "subjects" / SLUGS[0] / "subject.yaml"
    disputed.write_text("name: Física de este ordenador\nstyle_guide: null\n", encoding="utf-8")
    sync.flush()
    (other / "subjects" / SLUGS[0] / "subject.yaml").write_text(
        "name: Física del otro ordenador\nstyle_guide: null\n", encoding="utf-8"
    )
    other_vault = Vault.open_for_migration(other)
    other_sync = GitSync(other_vault, VaultGitSettings(), clock=ManualClock())

    with pytest.raises(MigrationRefusedError) as refused:
        migrate_to_users(other_vault, other_sync)

    message = str(refused.value)
    assert f"subjects/{SLUGS[0]}/subject.yaml" in message, "the path in conflict is named"
    assert MIGRATE_USERS_COMMAND in message
    assert not (other / "users").exists(), "a vault two PCs disagree on is not moved"
    assert git(other, "log", "-1", "--format=%s").strip() != migration_commit_subject(FIRST_USER)


def test_the_migration_pushes_the_commit_and_the_new_tags(
    vault: Vault, sync: GitSync, git_origin: Path
) -> None:
    report = migrate_to_users(vault, sync)

    assert report.pushed and report.push_failure is None
    assert report.sync_outcome == "ok"
    assert git(git_origin, "rev-parse", "main").strip() == report.commit
    assert git(git_origin, "tag", "--list").split() == [
        f"{FIRST_USER}/{TAG_V1}",
        f"{FIRST_USER}/{TAG_V2}",
        TAG_V1,
        TAG_V2,
    ], "the remote gets the versions under their user, and keeps the ones it had"


def test_a_push_that_fails_leaves_the_migration_done_and_says_so(
    vault: Vault, sync: GitSync
) -> None:
    report = migrate_to_users(vault, sync)  # this vault has no remote at all

    assert report.pushed is False
    assert report.push_failure is not None and report.push_failure.message
    assert report.migrated and Vault.open(vault.root).meta.legacy_root_user == FIRST_USER


def test_the_name_and_email_given_are_the_first_users(vault: Vault, sync: GitSync) -> None:
    report = migrate_to_users(vault, sync, name="Luis Martín", email="luis@instituto.es")

    assert (report.user_id, report.user_name) == ("luis-martin", "Luis Martín")
    migrated = Vault.open(vault.root)
    profile = get_user(migrated, "luis-martin")
    assert (profile.name, profile.email) == ("Luis Martín", "luis@instituto.es")
    assert migrated.meta.legacy_root_user == "luis-martin"
    assert [tag.name for tag in sync.for_user("luis-martin").list_notes_tags(*SLUGS)] == [
        f"luis-martin/{TAG_V1}",
        f"luis-martin/{TAG_V2}",
    ]


def test_a_name_or_an_email_that_is_not_one_refuses_the_migration(
    vault: Vault, sync: GitSync
) -> None:
    for kwargs in ({"name": "   "}, {"email": "no es un correo"}):
        before = content_of(vault.root)
        for dry_run in (False, True):
            with pytest.raises(UserProfileError):
                migrate_to_users(vault, sync, dry_run=dry_run, **kwargs)
        assert content_of(vault.root) == before


def test_a_vault_with_nothing_at_its_root_still_becomes_format_2_with_its_first_user(
    legacy_vault: Vault, sync: GitSync
) -> None:
    report = migrate_to_users(legacy_vault, sync)

    assert report.migrated
    assert (report.subjects, report.moved_files, report.tags) == ((), (), ())
    migrated = Vault.open(legacy_vault.root)
    assert migrated.meta.format_version == FORMAT_VERSION
    assert [profile.id for profile in list_users(migrated)] == [FIRST_USER]


def test_content_written_but_not_committed_yet_moves_too(vault: Vault, sync: GitSync) -> None:
    write_notes(vault, *SLUGS, f"{NOTES_V2}\nSin commitar.\n")

    migrate_to_users(vault, sync)

    moved = vault.root / "users" / FIRST_USER / NOTES
    assert moved.read_text(encoding="utf-8") == f"{NOTES_V2}\nSin commitar.\n"
    assert git(vault.root, "status", "--porcelain") == "", "the migration left nothing pending"


def test_a_user_handle_or_a_users_view_of_the_sync_is_refused(vault: Vault, sync: GitSync) -> None:
    migrate_to_users(vault, sync)
    migrated = Vault.open(vault.root)

    with pytest.raises(MigrationError, match="root handle"):
        migrate_to_users(migrated.for_user(FIRST_USER), sync)
    with pytest.raises(MigrationError, match="repository's"):
        migrate_to_users(migrated, sync.for_user(FIRST_USER))


@pytest.mark.skipif(os.geteuid() == 0, reason="root moves a file a read-only directory forbids")
def test_an_entry_git_cannot_move_is_left_where_it_is_and_named_in_the_report(
    vault: Vault, sync: GitSync
) -> None:
    subjects = vault.root / "subjects"
    subjects.chmod(0o500)  # nothing may be renamed out of it
    try:
        report = migrate_to_users(vault, sync)
    finally:
        subjects.chmod(0o755)

    assert report.left_behind == (f"subjects/{SLUGS[0]}",), "what did not move is named, not lost"
    assert report.moved_files, "the report still says what the move was about"
    assert (subjects / SLUGS[0] / "topics" / SLUGS[1] / "notes" / "apuntes.md").is_file()
    assert Vault.open(vault.root).meta.format_version == FORMAT_VERSION
