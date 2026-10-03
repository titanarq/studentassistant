"""One repository's sync as one user sees it: `GitSync.for_user` and what its view translates.

Git, the locks and the divergence refs belong to the repository whatever handle a sync was given,
and one `GitSync` keeps one state, one batch of pending changes and one push schedule for every
user of the vault. The view only scopes what names content: a path, relative to `users/<id>/`, and
a notes version tag, `<user-id>/<subject-slug>/<topic-slug>/apuntes-vN`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from git_helpers import ManualClock, clone_vault, git
from user_helpers import add_user

from studentassistant.config import VaultGitSettings
from studentassistant.vault import (
    GitCommandError,
    GitSync,
    RevertConflictError,
    UserGitSync,
    UserNotFoundError,
    Vault,
    create_subject,
    create_topic,
    notes_path,
    write_notes,
)
from studentassistant.vault.locking import LOCKS_DIRNAME, git_lock
from studentassistant.vault.sync import DIVERGENCE_LOCAL_REF, DIVERGENCE_REMOTE_REF
from studentassistant.vault.vault import VAULT_META_NAME

SUBJECT = "fisica"
TOPIC = "cinematica"
# The notes of a topic as a user's own id names them, which is what a view is given.
NOTES = f"subjects/{SUBJECT}/topics/{TOPIC}/notes/apuntes.md"
SUBJECT_FILE = f"subjects/{SUBJECT}/subject.yaml"


@pytest.fixture
def sync(tmp_vault: Vault, git_origin: Path) -> GitSync:
    """The repository's one sync: an origin to push to and a clock a test moves."""
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    sync.checkpoint("primer commit")
    return sync


@pytest.fixture
def ana(tmp_vault: Vault, sync: GitSync) -> UserGitSync:
    """A user with a physics topic of her own, as the repository's sync shows it to her."""
    user = add_user(tmp_vault, "ana", "Ana García")
    create_subject(user, "Física")
    create_topic(user, SUBJECT, "Cinemática")
    return sync.for_user("ana")


@pytest.fixture
def luis(tmp_vault: Vault, sync: GitSync) -> UserGitSync:
    """A second user with the same two slugs: the same id, another folder, other versions."""
    user = add_user(tmp_vault, "luis", "Luis Martín")
    create_subject(user, "Física")
    create_topic(user, SUBJECT, "Cinemática")
    return sync.for_user("luis")


def name_legacy_root_user(vault: Vault, user_id: str) -> None:
    """Say in `vault.yaml` whose content was the repository's own before there were folders.

    Written by hand because the field is #548's: a notes version committed before the move has to
    stay readable until then, and this is what tells a view whose it was.
    """
    meta = vault.root / VAULT_META_NAME
    meta.write_text(
        meta.read_text(encoding="utf-8") + f"legacy_root_user: {user_id}\n", encoding="utf-8"
    )


def test_the_notes_id_of_a_user_handle_is_the_path_its_view_reads(ana: UserGitSync) -> None:
    assert notes_path(ana.vault, SUBJECT, TOPIC).relative_to(ana.vault.path).as_posix() == NOTES
    assert ana.prefix == "users/ana/"
    assert ana.user_id == "ana"


def test_git_runs_in_the_repository_root_whichever_handle_the_sync_is_given(
    tmp_vault: Vault, git_origin: Path
) -> None:
    user = add_user(tmp_vault, "ana", "Ana García")
    create_subject(user, "Física")
    create_topic(user, SUBJECT, "Cinemática")
    write_notes(user, SUBJECT, TOPIC, "# las derivadas de Ana\n")
    sync = GitSync(user, VaultGitSettings(), clock=ManualClock())

    assert sync.checkpoint("los apuntes de Ana") is not None

    assert sync.git.root == tmp_vault.root
    listed = git(tmp_vault.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert f"users/ana/{NOTES}" in listed
    assert git(tmp_vault.root, "status", "--porcelain") == ""
    # The commit ran under the repository's lock, in the repository's own `.git/`: a user's folder
    # has none, and a lock with no file to live in would only bind the threads of this process.
    assert git_lock(user.path) is git_lock(tmp_vault.root)
    assert (
        git_lock(user.path).path == tmp_vault.root.resolve() / ".git" / LOCKS_DIRNAME / "git.lock"
    )
    assert git_lock(user.path).path is not None and git_lock(user.path).path.is_file()
    assert not (user.path / ".git").exists()


def test_a_divergence_of_a_user_handle_sync_is_kept_in_the_repository_root(
    tmp_vault: Vault, git_origin: Path, tmp_path: Path
) -> None:
    ana = add_user(tmp_vault, "ana", "Ana García")
    create_subject(ana, "Física")
    create_topic(ana, SUBJECT, "Cinemática")
    on_a = GitSync(ana, VaultGitSettings(), clock=ManualClock())
    on_a.flush()

    other_pc = clone_vault(git_origin, tmp_path / "pc-b")
    on_b = GitSync(other_pc.for_user("ana"), VaultGitSettings(), ManualClock())
    view_b = on_b.for_user("ana")
    (ana.path / SUBJECT_FILE).write_text("name: Física A\nstyle_guide: null\n", encoding="utf-8")
    on_a.flush()
    remote = git(tmp_vault.root, "rev-parse", "HEAD").strip()
    (other_pc.root / "users" / "ana" / SUBJECT_FILE).write_text(
        "name: Física B\nstyle_guide: null\n", encoding="utf-8"
    )
    local = on_b.checkpoint("cambio en B")

    result = on_b.sync()

    assert result.outcome == "conflict", result
    assert result.conflicts == (f"users/ana/{SUBJECT_FILE}",)
    divergence = on_b.status().divergence
    assert divergence is not None
    assert (divergence.local_commit, divergence.remote_commit) == (local, remote)
    assert git(other_pc.root, "rev-parse", DIVERGENCE_LOCAL_REF).strip() == local
    assert git(other_pc.root, "rev-parse", DIVERGENCE_REMOTE_REF).strip() == remote
    # The status reports the repository's paths; a view reads and answers with the user's own.
    versions = view_b.divergent_versions(SUBJECT_FILE)
    assert versions is not None
    assert versions.path == SUBJECT_FILE
    assert versions.local == "name: Física B\nstyle_guide: null\n"
    assert versions.remote == "name: Física A\nstyle_guide: null\n"
    assert view_b.divergent_versions(NOTES) is None


def test_two_users_versions_of_one_topic_count_apart(
    sync: GitSync, ana: UserGitSync, luis: UserGitSync
) -> None:
    first = ana.create_notes_tag(SUBJECT, TOPIC)
    write_notes(ana.vault, SUBJECT, TOPIC, "# Ana, segunda versión\n")
    ana.note_change()
    second = ana.create_notes_tag(SUBJECT, TOPIC, "apuntes revisados")
    other = luis.create_notes_tag(SUBJECT, TOPIC)
    unprefixed = sync.create_notes_tag(SUBJECT, TOPIC)

    assert [tag.name for tag in (first, second, other, unprefixed)] == [
        f"ana/{SUBJECT}/{TOPIC}/apuntes-v1",
        f"ana/{SUBJECT}/{TOPIC}/apuntes-v2",
        f"luis/{SUBJECT}/{TOPIC}/apuntes-v1",
        f"{SUBJECT}/{TOPIC}/apuntes-v1",
    ]
    assert [tag.version for tag in (first, second, other, unprefixed)] == [1, 2, 1, 1]
    assert first.commit != second.commit
    assert ana.list_notes_tags(SUBJECT, TOPIC) == [first, second]
    assert luis.list_notes_tags(SUBJECT, TOPIC) == [other]
    assert sync.list_notes_tags(SUBJECT, TOPIC) == [unprefixed]
    assert ana.list_notes_tags(SUBJECT, "dinamica") == []
    # A `NotesTag` still says what it said: which commit, when, and the message's first line.
    assert second.commit == git(sync.vault.root, "rev-parse", f"{second.name}^{{commit}}").strip()
    assert second.message == "apuntes revisados"
    assert second.tagged_at is not None and second.tagged_at.tzinfo is not None


@pytest.mark.parametrize(("subject", "topic"), [("fisica", "Cinemática"), ("ana/fisica", "x")])
def test_a_view_requires_slugs_too(ana: UserGitSync, subject: str, topic: str) -> None:
    with pytest.raises(ValueError):
        ana.create_notes_tag(subject, topic)
    with pytest.raises(ValueError):
        ana.list_notes_tags(subject, topic)


def test_read_file_at_reads_a_users_file_by_its_own_path(
    ana: UserGitSync, luis: UserGitSync
) -> None:
    write_notes(ana.vault, SUBJECT, TOPIC, "# las derivadas de Ana\n")
    ana.note_change()
    of_ana = ana.create_notes_tag(SUBJECT, TOPIC)
    write_notes(luis.vault, SUBJECT, TOPIC, "# las derivadas de Luis\n")
    luis.note_change()
    of_luis = luis.create_notes_tag(SUBJECT, TOPIC)

    assert ana.read_file_at(of_ana.name, NOTES) == "# las derivadas de Ana\n"
    assert ana.read_file_at(of_ana.commit, NOTES) == "# las derivadas de Ana\n"
    assert luis.read_file_at(of_luis.name, NOTES) == "# las derivadas de Luis\n"
    # One id, two files: at any revision each view reads its own user's.
    assert ana.read_file_at(of_luis.name, NOTES) == "# las derivadas de Ana\n"
    assert luis.read_file_at(of_ana.name, NOTES) is None  # Luis had no notes at that revision
    assert ana.read_file_at(of_ana.name, f"users/ana/{NOTES}") is None  # not a path of a view
    assert ana.read_file_at(of_ana.name, "subjects/fisica/topics/cinematica/notes/otro.md") is None
    for bad in ("", "/etc/passwd", "../luis/x.md", f"../{NOTES}"):
        with pytest.raises(ValueError):
            ana.read_file_at(of_ana.name, bad)
    with pytest.raises(ValueError):
        ana.read_file_at("--output=x", NOTES)


def test_read_file_at_reaches_the_content_of_before_the_users_folder(
    tmp_vault: Vault, sync: GitSync, ana: UserGitSync
) -> None:
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, SUBJECT, "Cinemática")
    write_notes(tmp_vault, SUBJECT, TOPIC, "# los apuntes de siempre\n")
    sync.note_change()
    legacy = sync.create_notes_tag(SUBJECT, TOPIC)
    add_user(tmp_vault, "luis", "Luis Martín")
    luis = sync.for_user("luis")

    assert ana.read_file_at(legacy.name, NOTES) is None  # nothing says whose that content was

    name_legacy_root_user(tmp_vault, "ana")

    assert ana.read_file_at(legacy.name, NOTES) == "# los apuntes de siempre\n"
    assert ana.read_file_at(legacy.commit, NOTES) == "# los apuntes de siempre\n"
    assert luis.read_file_at(legacy.name, NOTES) is None  # it was not his
    # Her own folder answers for a revision that has it; the root only fills in where it has not.
    write_notes(ana.vault, SUBJECT, TOPIC, "# los apuntes de Ana\n")
    ana.note_change()
    own = ana.create_notes_tag(SUBJECT, TOPIC)
    assert ana.read_file_at(own.name, NOTES) == "# los apuntes de Ana\n"
    assert ana.read_file_at(legacy.name, NOTES) == "# los apuntes de siempre\n"


def test_revert_paths_undoes_only_the_paths_of_the_user_that_asks(
    sync: GitSync, ana: UserGitSync, luis: UserGitSync
) -> None:
    write_notes(ana.vault, SUBJECT, TOPIC, "# Ana v1\n")
    write_notes(luis.vault, SUBJECT, TOPIC, "# Luis v1\n")
    ana.note_change()
    sync.flush()
    write_notes(ana.vault, SUBJECT, TOPIC, "# Ana v2\n")
    write_notes(luis.vault, SUBJECT, TOPIC, "# Luis v2\n")
    ana.note_change()
    commit = sync.checkpoint("las segundas versiones de los dos")
    assert commit is not None

    assert ana.revert_paths(commit, [NOTES], "Ana deshace su cambio") is not None

    assert (ana.vault.path / NOTES).read_text(encoding="utf-8") == "# Ana v1\n"
    assert (luis.vault.path / NOTES).read_text(encoding="utf-8") == "# Luis v2\n"


def test_a_revert_refused_on_a_users_path_names_it_as_the_user_gave_it(
    sync: GitSync, ana: UserGitSync
) -> None:
    write_notes(ana.vault, SUBJECT, TOPIC, "# Ana v1\n")
    ana.note_change()
    first = sync.checkpoint("v1")
    assert first is not None
    write_notes(ana.vault, SUBJECT, TOPIC, "# Ana v2\n")
    sync.checkpoint("v2")

    with pytest.raises(RevertConflictError) as refused:
        ana.revert_paths(first, [NOTES], "deshacer")

    assert refused.value.path == NOTES
    with pytest.raises(ValueError):
        ana.revert_paths(first, [f"../{NOTES}"], "deshacer")


def test_a_view_shares_the_state_commits_push_and_loop_of_its_parent(
    sync: GitSync, ana: UserGitSync, git_origin: Path
) -> None:
    write_notes(ana.vault, SUBJECT, TOPIC, "# las derivadas de Ana\n")
    ana.note_change()
    assert sync.status().pending_changes is True
    assert ana.status() == sync.status()

    commit = ana.checkpoint("los apuntes de Ana")

    assert commit is not None and commit == sync.status().last_commit
    assert sync.status().pending_changes is False
    assert not hasattr(ana, "run") and not hasattr(ana, "run_due"), (
        "the loop is the parent's: a view with one of its own would run a second batch and push"
    )
    ana.request_push()
    assert ana.status().next_push_due == sync.status().next_push_due

    tag = ana.create_notes_tag(SUBJECT, TOPIC, "apuntes revisados")
    assert sync.push_now()  # the parent's push carries the user's tag with the branch
    assert git(git_origin, "rev-parse", f"{tag.name}^{{commit}}").strip() == tag.commit
    assert ana.sync().outcome == "ok"
    assert ana.flush().last_push_at is not None


def test_a_sync_given_a_user_handle_still_gives_every_users_view(
    tmp_vault: Vault, git_origin: Path
) -> None:
    ana = add_user(tmp_vault, "ana", "Ana García")
    add_user(tmp_vault, "luis", "Luis Martín")
    sync = GitSync(ana, VaultGitSettings(), clock=ManualClock())

    view = sync.for_user("luis")

    assert view.parent is sync
    assert view.user_id == "luis"
    assert view.prefix == "users/luis/"
    assert view.vault.root == tmp_vault.root
    assert view.list_notes_tags(SUBJECT, TOPIC) == []


@pytest.mark.parametrize("user_id", ["no-existe", "../fuera", "Ana García", ""])
def test_a_view_needs_a_user_of_the_vault(sync: GitSync, user_id: str) -> None:
    with pytest.raises(UserNotFoundError):
        sync.for_user(user_id)


def test_the_git_lock_a_view_holds_is_the_repositorys(tmp_vault: Vault, git_origin: Path) -> None:
    ana = add_user(tmp_vault, "ana", "Ana García")
    create_subject(ana, "Física")
    held, release = threading.Event(), threading.Event()
    view = GitSync(ana, VaultGitSettings(timeout_seconds=0.2), clock=ManualClock()).for_user("ana")

    def hold() -> None:
        with view.locked():
            held.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        # The repository's own sync, on the root handle, is the one kept out by it.
        root = GitSync(tmp_vault, VaultGitSettings(timeout_seconds=0.2), clock=ManualClock())
        assert root.checkpoint("no entra") is None
        assert "otro proceso" in (root.status().last_error or "")
        with pytest.raises(GitCommandError, match="otro proceso"):
            root.list_notes_tags(SUBJECT, TOPIC)
    finally:
        release.set()
        holder.join(5)
