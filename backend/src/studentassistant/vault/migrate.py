"""Moving a format-1 vault into its first user's folder: one commit, and nothing lost.

Format 2 keeps every student's content under their own `users/<user-id>/` (epic #544, ADR-0002);
the vaults written before that hold theirs at the repository root, where it is nobody's, and
`Vault.open` refuses one until it has been moved (#548). `migrate_to_users` is that move, and
`studentassistant vault migrate-users` the command that runs it: the root's `subjects/` becomes the
first user's, in one commit, with the history git can still follow through the move and every notes
version still readable by the name it had.

What one run does, in the order it does it:

1. `sync.sync()`, which commits whatever is pending and rebases onto the remote. A migration moves
   the whole of a student's content at once, so it starts from a vault every PC of it agrees on: a
   `conflict` refuses and names the paths, because moving both sides of a divergence under a user
   would leave the student to settle it inside a folder neither PC wrote. An `offline` remote does
   not refuse -- what a migration does is local, and the push it ends with is retried by the next
   sync -- but the report says which of the two happened.
2. It refuses while any session of any topic is unended, and names them: a capture still writing a
   transcript would write it where the directory it is writing into just went. The sync of step 1
   has by then committed what that capture had written, which is what a sync does anyway and loses
   nothing; what has not happened is any part of the move.
3. `create_user` makes the first user -- named after `vault.yaml`'s `student` unless the caller
   gives a name -- and their `profile.json` with it.
4. `git mv` of every entry of the root `subjects/` into `users/<id>/subjects/`: a rename git
   records as one, never a copy and a delete, which is what keeps `git log --follow` of a moved
   `notes/apuntes.md` reaching the commits it had before, and what keeps moving a vault of
   photographs as cheap as moving one file.
5. `vault.yaml` is rewritten as format 2 naming `legacy_root_user: <id>`, the field `vault/sync.py`
   reads to know whose the notes versions tagged before the move were.
6. ONE commit, `migración: contenido al usuario <id>`, carries the moves, the profile and
   `vault.yaml` together: a vault is either migrated or it is not, and a reader that opened it
   between two commits would find content that is nobody's.
7. Every notes tag `<s>/<t>/apuntes-vN` is re-created as the annotated tag
   `<id>/<s>/<t>/apuntes-vN` on that commit, with the same version and the same message, the old
   tag kept where it was (`sync.retag_notes`): a study label names the tag it was made from, and a
   version a student read by its tag keeps reading.
8. `push_now()`, so the other PCs of the vault get the move and its tags. A push that fails undoes
   nothing; the report carries the failure and the next sync retries it.

`dry_run=True` answers what a run would do and changes nothing: no `sync()` (which commits), no
user, no move, no commit, no tag. It plans against the vault as this PC holds it, so the one thing
a dry run cannot see is what the remote has and this PC has not pulled.

A vault that is already format 2 is not an error: the run does nothing and says so, which is what
makes a second `migrate-users` -- the one a student who did not read the first output runs --
harmless.

Nothing here rebuilds the derived index, and nothing here reopens the vault: the handle a migration
is given keeps the `meta` it was opened with, so a caller that goes on using the vault opens it
again, and the CLI rebuilds the indexes after a migration that worked (one database per user since
#547). Stop the backend first, which is what the CLI's help says: it holds a handle on a vault this
move makes stale, and it would commit into a repository whose content is on its way to another
directory.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import write_yaml_atomic
from studentassistant.vault.locking import VaultBusyError
from studentassistant.vault.models import FORMAT_VERSION, LEGACY_FORMAT_VERSION, VaultMeta
from studentassistant.vault.sessions import list_sessions
from studentassistant.vault.slugs import is_slug
from studentassistant.vault.subjects import SUBJECTS_DIRNAME, SubjectError, subject_slugs
from studentassistant.vault.sync import (
    GitSync,
    NotesTag,
    PushFailure,
    SyncOutcome,
    SyncResult,
    UserGitSync,
    notes_tag_name,
)
from studentassistant.vault.topics import TopicError, topic_slugs
from studentassistant.vault.users import UserProfile, create_user, prospective_user
from studentassistant.vault.vault import (
    MIGRATE_USERS_COMMAND,
    USERS_DIRNAME,
    VAULT_META_NAME,
    Vault,
)

logger = logging.getLogger(__name__)

MIGRATION_COMMIT_PREFIX = "migración:"
"""What the one commit a migration makes starts with, so `git log --grep` finds it, as
`purge.PURGE_COMMIT_PREFIX` does for a purge's."""

MAX_NAMED_ITEMS = 10
"""How many paths or sessions a Spanish refusal names before it says how many more there are."""


class MigrationError(VaultError):
    """A migration that could not be planned or completed; the message says why."""


class MigrationRefusedError(MigrationError):
    """A vault this backend will not migrate in the state it is in; the message is Spanish.

    Apart from `MigrationError` because this is the one a caller shows as it is: what it says is
    for the student whose vault it is -- a conflict to settle, a capture to end, a backend to stop
    -- and not for whoever reads a log.
    """


@dataclass(frozen=True)
class RetaggedNotes:
    """One notes version a migration names inside its user's folder.

    `old_name` is the tag as the repository held it (`fisica/cinematica/apuntes-v2`), `new_name`
    the one that user's handle writes and lists (`ana/fisica/cinematica/apuntes-v2`), and both keep
    pointing at the notes they were made for: the old one at the commit it was on, the new one at
    the migration's. `created` says whether this run made the new one and `commit` which one it is
    on -- empty in a dry run, which makes none, and for a tag whose name was taken already.
    """

    subject: str
    topic: str
    version: int
    old_name: str
    new_name: str
    message: str
    commit: str = ""
    created: bool = False


@dataclass(frozen=True)
class MigrationReport:
    """What one `migrate_to_users` did, or -- with `dry_run` -- what it would do.

    `migrated` is whether a migration commit was made: `False` in a dry run and in a vault that was
    already format 2, whose `reason` says so in Spanish. `moved_files` are the repository-relative
    paths the move took, named as they were before it (`subjects/...`), and `left_behind` the
    entries of the root `subjects/` that did not move because git tracks nothing in them: they are
    still on disk, at the root, and a caller names them so that nothing a student had there is
    quietly left out of their folder.
    """

    dry_run: bool
    migrated: bool
    user_id: str | None = None
    user_name: str | None = None
    subjects: tuple[str, ...] = ()
    moved_files: tuple[str, ...] = ()
    tags: tuple[RetaggedNotes, ...] = ()
    commit: str | None = None
    sync_outcome: SyncOutcome | None = None
    sync_message: str = ""
    pushed: bool | None = None
    push_failure: PushFailure | None = None
    left_behind: tuple[str, ...] = ()
    reason: str | None = None


def migration_commit_subject(user_id: str) -> str:
    """The subject of the one commit a migration makes."""
    return f"{MIGRATION_COMMIT_PREFIX} contenido al usuario {user_id}"


def migrate_to_users(
    vault: Vault,
    sync: GitSync,
    *,
    name: str | None = None,
    email: str | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Turn the format-1 `vault` into a format-2 one whose first user owns the root's content.

    `vault` is the handle `Vault.open_for_migration` gives, the only one that opens a format-1
    vault, and `sync` the repository's own `GitSync`, every step of a migration being the whole
    repository's. `name` and `email` are the first user's, and `name` defaults to `vault.yaml`'s
    `student`: the content at the root is that student's, since it is the name they gave
    `setup --create`.

    Returns what it did as a `MigrationReport`; a dry run's says what a run would do, and changes
    nothing.

    Raises:
        MigrationRefusedError: when the vault is in a state this backend will not migrate -- the
            sync came back with a conflict, a session of some topic is unended, another process
            holds the repository's git lock, or the user's folder is there already. The message is
            Spanish and says what to do. No migration was made and the vault is still format 1 with
            its content at the root; the one thing a run that is not a dry one has done by then is
            the commit of what was pending, which the sync it starts with makes and which loses
            nothing.
        MigrationError: when `vault` is not a root handle, `sync` is one user's view of the
            repository's, or the migration commit did not happen. A commit that fails leaves the
            moves and `vault.yaml` on disk, in the layout that file then names, so the vault still
            reads and the next sync commits what the migration staged; what it will not have is
            the re-created tags.
        UserProfileError: when `name` or `email` is not one a profile accepts; nothing is written.
        SubjectError, TopicError, SessionFileError: when a topic's own file reads but a session it
            lists does not, which is a vault to fix before moving all of it.
        GitCommandError: when git failed a step, or another process kept it busy past the sync's
            timeout.
    """
    _require_repository(vault, sync)
    if vault.meta.format_version != LEGACY_FORMAT_VERSION:
        return MigrationReport(
            dry_run=dry_run, migrated=False, reason=_already_migrated_message(vault)
        )
    student = vault.meta.student if name is None else name
    if dry_run:
        profile, plan = _planned(vault, sync, student, email)
        return MigrationReport(
            dry_run=True,
            migrated=False,
            user_id=profile.id,
            user_name=profile.name,
            subjects=plan.subjects,
            moved_files=plan.files,
            tags=_retagged_notes(plan, "", frozenset()),
        )
    synced = _synced(sync)
    profile, plan = _planned(vault, sync, student, email)
    return _applied(vault, sync, profile, plan, synced)


def _require_repository(vault: Vault, sync: GitSync) -> None:
    """Refuse a handle or a sync that is one user's: a migration is the whole repository's.

    A view's notes tags are that user's prefixed ones, so a migration given one would look for the
    repository's unprefixed tags through it, find none, and move the content leaving every notes
    version behind -- silently, which is the one way this could lose something.

    Raises:
        MigrationError: when `vault` is a user handle, or `sync` a `UserGitSync`.
    """
    if vault.user_id is not None:
        raise MigrationError(
            f"this handle is the one of user {vault.user_id!r}: migrating a vault needs the root"
            " handle, the one Vault.open_for_migration returns"
        )
    if isinstance(sync, UserGitSync):
        raise MigrationError(
            f"this sync is user {sync.user_id!r}'s view: migrating a vault needs the repository's"
            " own GitSync, whose notes tags are the unprefixed ones a migration copies"
        )


def _synced(sync: GitSync) -> SyncResult:
    """`sync.sync()`, refusing the migration on the one outcome that forbids going ahead.

    Raises:
        MigrationRefusedError: when the pull left a conflict, whose paths the message names.
    """
    result = sync.sync()
    if result.outcome == "conflict":
        raise MigrationRefusedError(_conflict_message(result.conflicts))
    return result


def _planned(
    vault: Vault, sync: GitSync, student: str, email: str | None
) -> tuple[UserProfile, _Plan]:
    """The user a migration would create and the move it would make, refusing what forbids both.

    Raises:
        MigrationRefusedError: when a session of some topic is unended.
        UserProfileError: when `student` or `email` is not one a profile accepts.
    """
    profile = prospective_user(vault, student, email)
    unended = _unended_sessions(vault)
    if unended:
        raise MigrationRefusedError(_unended_sessions_message(unended))
    return profile, _Plan.of(vault, sync, profile.id)


def _unended_sessions(vault: Vault) -> list[str]:
    """`<subject>/<topic>/<session-id>` of every session of the vault that has not ended.

    A topic whose `subject.yaml` or `topic.yaml` this backend cannot read is skipped instead of
    refused: a session cannot be started in a topic that cannot be read (`start_session` requires
    it), so the half-created one -- the directory a crash left before its `topic.yaml` -- holds no
    open session to wait for. A session the topic does list and whose own `session.yaml` cannot be
    read is left to raise, because that one may well be open and a vault with an unreadable session
    is one to fix before moving all of it.
    """
    unended: list[str] = []
    for subject in subject_slugs(vault):
        for topic in topic_slugs(vault, subject):
            try:
                sessions = list_sessions(vault, subject, topic)
            except (SubjectError, TopicError):
                continue
            unended.extend(
                f"{subject}/{topic}/{meta.id}" for meta in sessions if meta.ended_at is None
            )
    return unended


def _applied(
    vault: Vault,
    sync: GitSync,
    profile: UserProfile,
    plan: _Plan,
    synced: SyncResult,
) -> MigrationReport:
    """The migration itself: the user, the move, `vault.yaml`, one commit, the tags, the push.

    Raises:
        MigrationRefusedError: when another process holds the repository's git lock, or the user's
            folder is there already.
        MigrationError: when the commit did not happen.
    """
    user_id = profile.id
    try:
        with sync.locked():
            try:
                create_user(vault, profile.name, profile.email)
            except FileExistsError as taken:
                raise MigrationRefusedError(_user_folder_message(user_id)) from taken
            left_behind = _move_subjects(sync, plan)
            _forget_empty_subjects_root(vault)
            write_yaml_atomic(vault.root / VAULT_META_NAME, _migrated_meta(vault.meta, user_id))
            commit = sync.checkpoint(migration_commit_subject(user_id))
            if commit is None:
                raise MigrationError(
                    "the migration commit did not happen:"
                    f" {sync.status().last_error or 'git refused it'}"
                )
            created = _retag(sync, plan, commit)
    except VaultBusyError as busy:
        raise MigrationRefusedError(_busy_message(busy)) from busy
    pushed = sync.push_now()
    return MigrationReport(
        dry_run=False,
        migrated=True,
        user_id=user_id,
        user_name=profile.name,
        subjects=plan.subjects,
        moved_files=plan.files,
        tags=_retagged_notes(plan, commit, created),
        commit=commit,
        sync_outcome=synced.outcome,
        sync_message=synced.message,
        pushed=pushed,
        push_failure=None if pushed else sync.status().last_push_failure,
        left_behind=tuple(left_behind),
    )


def _move_subjects(sync: GitSync, plan: _Plan) -> list[str]:
    """`git mv` every entry the plan moves into the user's `subjects/`; the refused ones come back.

    One rename per entry rather than one of the root's `subjects/` itself, because `create_user`
    has just made the destination -- `users/<id>/subjects/`, with the `.gitkeep` that keeps an
    empty one alive in a clone -- and `git mv` into a directory that exists would put the root's
    `subjects/` inside it. A rename git refuses after the plan said it could move one is left where
    it is, logged and named in the report: stopping halfway would leave a vault whose content is in
    two places and whose `vault.yaml` says neither.
    """
    root = f"{SUBJECTS_DIRNAME}/"
    destination = f"{USERS_DIRNAME}/{plan.user_id}/{SUBJECTS_DIRNAME}"
    left_behind: list[str] = []
    for relative in plan.moves:
        result = sync.git.run("mv", "--", relative, f"{destination}/{relative.removeprefix(root)}")
        if not result.ok:
            left_behind.append(relative)
            logger.warning("vault migration: `git mv %s` failed: %s", relative, result.describe())
    return left_behind


def _forget_empty_subjects_root(vault: Vault) -> None:
    """Remove the root's `subjects/` once the move has emptied it: format 2 keeps none.

    Git renames what is inside a directory and leaves the directory itself, and an empty `subjects/`
    at the root of a migrated vault is a leftover a reader would have to think about: it is where
    the content of format 1 lived, and it is now nowhere's. One that is not empty stays -- an entry
    git could not move is still the student's, and the report names it -- and so does one this
    process may not write to: tidying a folder that holds nothing git knows is not worth failing a
    migration that has already moved everything else.
    """
    try:
        (vault.root / SUBJECTS_DIRNAME).rmdir()
    except OSError:
        logger.debug("vault migration: the root's %s stayed", SUBJECTS_DIRNAME, exc_info=True)


def _migrated_meta(meta: VaultMeta, user_id: str) -> VaultMeta:
    """`vault.yaml` as a migration writes it: this layout, naming whose the root's content is.

    `legacy_root_user` is what keeps the notes versions tagged before the move readable: a user's
    `read_file_at` that finds no `users/<id>/...` at a revision reads the root's path instead, for
    the user this names and for no other (`vault/sync.py`, #547).
    """
    return meta.model_copy(update={"format_version": FORMAT_VERSION, "legacy_root_user": user_id})


def _retag(sync: GitSync, plan: _Plan, commit: str) -> set[str]:
    """Re-create every notes tag the plan found under the user's prefix, on the migration commit.

    Returns the names it made; one that was taken already is not in it. The caller holds the git
    lock, so the tags land on the commit the migration just made and not on whatever a background
    batch of the sync's commits next -- which is also why they are made before the push that
    carries them.
    """
    created: set[str] = set()
    for group in plan.retag:
        created.update(
            tag.name
            for tag in sync.retag_notes(
                plan.user_id, group.subject, group.topic, group.tags, commit
            )
        )
    return created


def _retagged_notes(plan: _Plan, commit: str, created: set[str]) -> tuple[RetaggedNotes, ...]:
    """The report's tags: what the plan found, and which of it this run made."""
    notes: list[RetaggedNotes] = []
    for group in plan.retag:
        for tag in group.tags:
            new_name = notes_tag_name(group.subject, group.topic, tag.version, plan.user_id)
            made = new_name in created
            notes.append(
                RetaggedNotes(
                    subject=group.subject,
                    topic=group.topic,
                    version=tag.version,
                    old_name=tag.name,
                    new_name=new_name,
                    message=tag.message,
                    commit=commit if made else "",
                    created=made,
                )
            )
    return tuple(notes)


@dataclass(frozen=True)
class _RetagGroup:
    """One topic's notes versions as the repository holds them: what a migration copies."""

    subject: str
    topic: str
    tags: tuple[NotesTag, ...]


@dataclass(frozen=True)
class _Plan:
    """What one migration would move and re-tag, enumerated before anything is written."""

    user_id: str
    # The entries of the root `subjects/` that move, repository-relative (`subjects/fisica`).
    moves: tuple[str, ...]
    # The directories of them, which is to say the subject slugs.
    subjects: tuple[str, ...]
    # Every file on disk under a moving entry, repository-relative, as it was before the move.
    files: tuple[str, ...]
    retag: tuple[_RetagGroup, ...]

    @classmethod
    def of(cls, vault: Vault, sync: GitSync, user_id: str) -> _Plan:
        """Enumerate the move: what is on disk under the root's `subjects/`, and its notes tags.

        Everything on disk counts, tracked or not, because the `sync()` a migration starts with has
        just committed what was pending: by the time anything moves, git tracks all of it. That is
        also what makes a dry run's answer the real run's -- it enumerates the same files, and does
        not commit them.
        """
        root = vault.path / SUBJECTS_DIRNAME
        moves: list[str] = []
        subjects: list[str] = []
        files: list[str] = []
        for entry in sorted(root.iterdir()) if root.is_dir() else []:
            relative = f"{SUBJECTS_DIRNAME}/{entry.name}"
            moves.append(relative)
            if entry.is_dir():
                subjects.append(entry.name)
            files.extend(_files_under(entry, relative))
        return cls(
            user_id=user_id,
            moves=tuple(moves),
            subjects=tuple(subjects),
            files=tuple(sorted(files)),
            retag=_retag_groups(vault, sync),
        )


def _files_under(entry: Path, relative: str) -> list[str]:
    """Every file on disk under `entry`, as the repository-relative paths it has now."""
    if entry.is_file():
        return [relative]
    return sorted(
        f"{relative}/{found.relative_to(entry).as_posix()}"
        for found in entry.rglob("*")
        if found.is_file()
    )


def _retag_groups(vault: Vault, sync: GitSync) -> tuple[_RetagGroup, ...]:
    """The notes tags of every topic of the vault, by topic, oldest version first.

    A subject or a topic whose directory is not a slug moves with the rest of the content and is
    skipped here: no notes tag was ever made for it, because `create_notes_tag` refuses a name that
    is not a slug, and asking git for one would only be asking for a `ValueError`.
    """
    groups: list[_RetagGroup] = []
    for subject in subject_slugs(vault):
        if not is_slug(subject):
            continue
        for topic in topic_slugs(vault, subject):
            if not is_slug(topic):
                continue
            tags = tuple(sync.list_notes_tags(subject, topic))
            if tags:
                groups.append(_RetagGroup(subject=subject, topic=topic, tags=tags))
    return tuple(groups)


def _already_migrated_message(vault: Vault) -> str:
    """The Spanish "there was nothing to do" of a vault that is already format 2."""
    return (
        f"El vault de {vault.root} ya es de formato {vault.meta.format_version}: su contenido ya"
        " está en la carpeta de cada usuario, así que no hay nada que migrar."
    )


def _conflict_message(conflicts: Sequence[str]) -> str:
    """The Spanish refusal of a vault this PC and its remote do not agree on."""
    return (
        "No se puede migrar: la sincronización con el remoto ha dejado un conflicto sin resolver"
        f" en {_names(conflicts)}. La migración mueve todo el contenido de una vez, y no debe"
        " hacerlo mientras este ordenador y el remoto guardan dos versiones distintas de los mismos"
        f" ficheros: resuelve el conflicto y vuelve a ejecutar `{MIGRATE_USERS_COMMAND}`."
    )


def _unended_sessions_message(unended: Sequence[str]) -> str:
    """The Spanish refusal of a vault that is still capturing, naming the sessions that are."""
    if len(unended) == 1:
        state, instruction = f"la sesión {unended[0]} sigue abierta", "Termínala"
    else:
        state = f"hay {len(unended)} sesiones sin terminar ({_names(unended)})"
        instruction = "Termínalas"
    return (
        f"No se puede migrar: {state}. {instruction} y vuelve a ejecutar"
        f" `{MIGRATE_USERS_COMMAND}`: la migración mueve todo el contenido en un único commit, y no"
        " debe hacerlo mientras una captura está escribiendo en el sitio que se está moviendo."
    )


def _user_folder_message(user_id: str) -> str:
    """The Spanish refusal of a vault whose first user's folder is there already."""
    return (
        f"No se puede migrar: la carpeta users/{user_id} ya existe en el vault, que es lo que deja"
        " una migración interrumpida a medias. No se ha cambiado nada: comprueba de quién es ese"
        f" contenido y vuelve a ejecutar `{MIGRATE_USERS_COMMAND}` cuando esté claro."
    )


def _busy_message(busy: VaultBusyError) -> str:
    """The Spanish refusal of a vault whose git another process is using."""
    return (
        f"No se ha podido migrar: otro proceso tiene ocupado el git del vault ({busy}). Detén el"
        f" backend y vuelve a ejecutar `{MIGRATE_USERS_COMMAND}`."
    )


def _names(items: Sequence[str], limit: int = MAX_NAMED_ITEMS) -> str:
    """The first `limit` of `items`, named, and how many more there are."""
    listed = ", ".join(items[:limit])
    return listed if len(items) <= limit else f"{listed} y {len(items) - limit} más"


__all__ = [
    "MIGRATION_COMMIT_PREFIX",
    "MigrationError",
    "MigrationRefusedError",
    "MigrationReport",
    "RetaggedNotes",
    "migrate_to_users",
    "migration_commit_subject",
]
