"""The vault directory itself: how one is created, and how an existing one is recognised.

A vault is a git repository (ADR-0002) whose first two files are `vault.yaml` -- which layout it
uses, since when, and whose it is -- and `.gitattributes`, which tells git to merge the append-only
JSONL files by keeping both sides' lines instead of asking the student to resolve a conflict inside
a transcript. Both are written before `git init -b main`, so the repository is born on the branch
the GitHub remote and every later commit expect, and already holds what says which vault it is.

Deciding whether a directory IS a vault is `Vault.open`'s job, and it refuses a doubtful one with an
error that names the reason: the vault is the only place this backend writes content, and opening
the wrong directory would mean writing a student's notes on top of somebody else's files.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.files import read_yaml, write_text_atomic, write_yaml_atomic
from studentassistant.vault.models import FORMAT_VERSION, VaultMeta

VAULT_META_NAME = "vault.yaml"
GITATTRIBUTES_NAME = ".gitattributes"
MAIN_BRANCH = "main"

GITATTRIBUTES_CONTENT = "*.jsonl merge=union\n"


class VaultError(Exception):
    """A vault this backend refuses to create or to use; the message says why."""


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
    """An open vault: the directory it lives in and the `vault.yaml` that says what it is."""

    path: Path
    meta: VaultMeta

    @classmethod
    def init(cls, path: Path, student: str) -> Vault:
        """Create the vault at `path`: its directory, its two first files and its git repository.

        `student` is the display name `vault.yaml` records, which is what the web UI and the editor
        call the student by; nothing else here depends on it. The caller decides where the vault
        lives (`studentassistant.config` holds the default), and this makes the directory itself,
        parents included, so a first run does not need one to exist already.

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
        return cls(path=path, meta=meta)

    @classmethod
    def open(cls, path: Path) -> Vault:
        """Read the vault at `path`, and refuse anything that is not one.

        Opens it read-only: nothing here writes, so a vault this backend cannot fully understand is
        left exactly as it was found.

        Raises:
            VaultNotFoundError: when `path` is not an existing directory.
            VaultMetaError: when `vault.yaml` is missing, unreadable, not YAML, or holds fields
                that are not a `VaultMeta`.
            VaultFormatError: when `vault.yaml` declares a `format_version` other than the one this
                backend reads and writes.
        """
        if not path.is_dir():
            raise VaultNotFoundError(f"{path} is not a vault: there is no such directory")
        return cls(path=path, meta=_read_meta(path / VAULT_META_NAME))


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
