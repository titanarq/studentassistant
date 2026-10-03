"""The vault directory itself: how one is created, and how an existing one is recognised.

A vault is a git repository (ADR-0002) whose first two files are `vault.yaml` -- which layout it
uses, since when, and whose it is -- and `.gitattributes`, which tells git to merge the append-only
JSONL files by keeping both sides' lines instead of asking the student to resolve a conflict inside
a transcript. Both are written before `git init -b main`, so the repository is born on the branch
the GitHub remote and every later commit expect, and already holds what says which vault it is.

Deciding whether a directory IS a vault is `Vault.open`'s job, and it refuses a doubtful one with an
error that names the reason: the vault is the only place this backend writes content, and opening
the wrong directory would mean writing a student's notes on top of somebody else's files.

An open vault is a handle on two directories (ADR-0002, epic #544): `root`, the git repository, and
`path`, where content is written. `Vault.init` and `Vault.open` give a ROOT handle, whose two
directories are the one it was given; `for_user` gives a USER handle, whose `path` is
`users/<user-id>/` of the same repository while `root` stays the repository itself. Every reader
and writer of this package builds its paths from `vault.path`, so handing it a user handle is what
keeps one student's subjects, sessions, sources and notes inside that student's folder, and the
vault-relative ids they return stay relative to it. What belongs to the repository as a whole
rather than to one user -- git itself, the locks, `.sa/active.yaml`, the feedback inbox -- is
`root`'s business, and every module that reads or writes one of them does it there, whichever of
the two handles it was given (#547).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.errors import UserNotFoundError, VaultError
from studentassistant.vault.files import read_yaml, write_text_atomic, write_yaml_atomic
from studentassistant.vault.models import FORMAT_VERSION, VaultMeta
from studentassistant.vault.slugs import is_slug

VAULT_META_NAME = "vault.yaml"
GITATTRIBUTES_NAME = ".gitattributes"
MAIN_BRANCH = "main"

# One folder per user, under the repository root: `users/<user-id>/` holds that user's profile and
# every subject, topic, session and note of theirs (`docs/modules/vault.md`, "Layout").
USERS_DIRNAME = "users"
# The file that says a user exists, and whose fields `vault/users.py` owns; `for_user` only checks
# that it is there, because a folder without it is not a user this backend may write into.
USER_PROFILE_NAME = "profile.json"

ACTIVE_HOST_NAME = ".sa/active.yaml"
# The merge driver of `.sa/active.yaml`: `true` keeps the upstream side of a rebase, i.e. the
# remote's record, which is the one the session start must see (`vault/active.py`). Git knows the
# driver only because the pull defines it on its command line (`vault/sync.py`).
ACTIVE_HOST_MERGE_DRIVER = "sa-active"
ACTIVE_HOST_GITATTRIBUTES_LINE = f"{ACTIVE_HOST_NAME} merge={ACTIVE_HOST_MERGE_DRIVER}\n"

GITATTRIBUTES_CONTENT = "*.jsonl merge=union\n" + ACTIVE_HOST_GITATTRIBUTES_LINE


class VaultNotFoundError(VaultError):
    """There is no vault at the path given."""


class VaultMetaError(VaultError):
    """`vault.yaml` is missing, or holds something this backend cannot read as a `VaultMeta`."""


class VaultFormatError(VaultMetaError):
    """`vault.yaml` declares a layout version this backend does not understand.

    Reading a vault a newer backend wrote and writing it back would drop every field this one does
    not declare, so refusing it is the whole point of `format_version`.
    """


@dataclass(frozen=True)
class Vault:
    """An open vault: the directory its content lives in, the repository holding it, and the
    `vault.yaml` that says what it is.

    A ROOT handle (`Vault.init`, `Vault.open`) has `path == root` and `user_id is None`: its
    content is the repository's own. A USER handle (`for_user`) has the same `root` and `meta` but
    a `path` of `root / "users" / user_id`, and every path a writer of this package builds from
    `vault.path` -- and every vault-relative id it returns -- is then inside that one user's
    folder.
    """

    path: Path
    meta: VaultMeta
    root: Path
    user_id: str | None = None

    @classmethod
    def init(cls, path: Path, student: str) -> Vault:
        """Create the vault at `path`: its directory, its two first files and its git repository.

        `student` is the display name `vault.yaml` records, which is what the web UI and the editor
        call the student by; nothing else here depends on it. The caller decides where the vault
        lives (`studentassistant.config` holds the default), and this makes the directory itself,
        parents included, so a first run does not need one to exist already.

        The handle returned is a root one: it creates no user, so it writes nothing under
        `users/`.

        Raises:
            FileExistsError: when `path` already exists, so that a typo cannot turn a directory
                that already holds something into a vault.
            subprocess.CalledProcessError: when `git init` refuses the directory; the two files are
                already there and `Vault.open` will still read them.
        """
        path.mkdir(parents=True)
        meta = VaultMeta(created_at=datetime.now(UTC), student=student)
        write_yaml_atomic(path / VAULT_META_NAME, meta)
        write_text_atomic(path / GITATTRIBUTES_NAME, GITATTRIBUTES_CONTENT)
        _run_git(path, "init", "-b", MAIN_BRANCH)
        return cls(path=path, meta=meta, root=path)

    @classmethod
    def open(cls, path: Path) -> Vault:
        """Read the vault at `path`, and refuse anything that is not one.

        Opens it read-only: nothing here writes, so a vault this backend cannot fully understand is
        left exactly as it was found. The handle returned is a root one; `for_user` narrows it to
        the folder of one user.

        Raises:
            VaultNotFoundError: when `path` is not an existing directory.
            VaultMetaError: when `vault.yaml` is missing, unreadable, not YAML, or holds fields
                that are not a `VaultMeta`.
            VaultFormatError: when `vault.yaml` declares a `format_version` other than the one this
                backend reads and writes.
        """
        if not path.is_dir():
            raise VaultNotFoundError(f"{path} is not a vault: there is no such directory")
        return cls(path=path, meta=_read_meta(path / VAULT_META_NAME), root=path)

    def for_user(self, user_id: str) -> Vault:
        """The handle on one user's content: this same vault, scoped to `users/<user_id>/`.

        Nothing is read but the check that the user exists, and nothing is written: the handle is
        what the callers pass to the readers and writers, which do the rest inside it.

        Raises:
            ValueError: when this is already a user handle. A user's folder holds no `users/` of
                its own, so a handle two users deep would name a directory that is nobody's
                content, and reading one back would be silent about it.
            UserNotFoundError: when `user_id` is not a slug -- refused before any path is built or
                looked at, so an id that came from a URL cannot name a directory outside `users/`
                -- or when `users/<user_id>/profile.json` is not there.
        """
        if self.user_id is not None:
            raise ValueError(
                f"this handle is already the one of user {self.user_id!r}: for_user needs a root"
                " handle, the one Vault.open returns"
            )
        if not is_slug(user_id):
            raise UserNotFoundError(f"{user_id!r} is not a user id")
        user_path = self.root / USERS_DIRNAME / user_id
        if not (user_path / USER_PROFILE_NAME).is_file():
            raise UserNotFoundError(
                f"there is no user {user_id!r} in this vault: {user_path / USER_PROFILE_NAME} is"
                " not there"
            )
        return replace(self, path=user_path, user_id=user_id)


def _read_meta(meta_path: Path) -> VaultMeta:
    """Read `vault.yaml`, naming in the error which of the ways it can be wrong it is.

    Raises:
        VaultMetaError: when the file is missing, unreadable, not YAML or not a `VaultMeta`.
        VaultFormatError: when it is a `VaultMeta` of a `format_version` this backend refuses.
    """
    try:
        return read_yaml(meta_path, VaultMeta)
    except FileNotFoundError as error:
        raise VaultMetaError(
            f"{meta_path} is missing, so {meta_path.parent} is not a vault"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise VaultMetaError(f"{meta_path} cannot be read: {error}") from error
    except YAMLError as error:
        raise VaultMetaError(f"{meta_path} is not YAML this backend can parse: {error}") from error
    except ValidationError as error:
        raise _invalid_meta_error(meta_path, error) from error


def _invalid_meta_error(meta_path: Path, error: ValidationError) -> VaultMetaError:
    """Say whether `vault.yaml` is a layout of another version, or simply not a vault at all."""
    for failure in error.errors(include_input=True):
        if failure["loc"] == ("format_version",):
            return VaultFormatError(
                f"{meta_path} declares format_version {failure['input']!r}, and this backend reads"
                f" and writes {FORMAT_VERSION}: the vault needs the version that wrote it"
            )
    return VaultMetaError(f"{meta_path} does not hold a vault this backend understands: {error}")


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run `git` inside the vault: this module is the only one that runs git on it (ADR-0002)."""
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
