"""The model behind every YAML file the vault writes, in the layout of `docs/modules/vault.md`.

The fields are declared in the order the file shows them, because that order is what the
deterministic dump writes and what keeps the git diffs of the vault small (ADR-0002).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORMAT_VERSION = 2
"""The layout this backend writes: every student's content under their own `users/<user-id>/`.

The repository root keeps only what belongs to it -- `vault.yaml`, `.gitattributes`,
`.sa/active.yaml`, the locks and `feedback/inbox.jsonl` (epic #544, `docs/modules/vault.md`).
"""

LEGACY_FORMAT_VERSION = 1
"""The layout this backend still reads, and reads only so that it can be migrated.

A format-1 vault holds its content at the repository root, which no user owns: `Vault.open`
refuses one with a Spanish message naming `studentassistant vault migrate-users`, and
`Vault.open_for_migration` is the one way to open it (#548).
"""

DEFAULT_FIDELITY_MODE = "estricto"


class VaultFileModel(BaseModel):
    """What every model backed by a vault file has in common."""

    # The vault is the source of truth, so a key this backend does not declare means the file was
    # written by a newer one: refusing it is better than dropping the student's content on the next
    # read-modify-write. Which layout versions are readable at all is `format_version`'s job.
    model_config = ConfigDict(extra="forbid")


class VaultMeta(VaultFileModel):
    """`vault.yaml`: which layout the vault uses, since when, and whose it is.

    `legacy_root_user` is the id of the user who received the content a format-1 vault had at its
    root, and is `None` in every vault born with this layout. The notes versions committed before
    that move are tagged without a user prefix, so this is the id `vault/sync.py` reads to know
    whose they were, and the one `vault migrate-users` writes when it makes the move (#548).
    """

    format_version: int = FORMAT_VERSION
    created_at: datetime
    student: str
    legacy_root_user: str | None = None

    @field_validator("format_version")
    @classmethod
    def known_format_version(cls, format_version: int) -> int:
        """Accept the layout this backend writes and the one it migrates; refuse every other.

        Which of the two a vault is, and what a caller may do with each, is `Vault.open`'s and
        `Vault.open_for_migration`'s business: reading a format-1 `vault.yaml` back into a model is
        what lets the migration learn the `student` the first user is named after.
        """
        if format_version not in (LEGACY_FORMAT_VERSION, FORMAT_VERSION):
            raise ValueError(
                f"unsupported vault format version {format_version}"
                f" (this backend reads and writes {FORMAT_VERSION}, and migrates"
                f" {LEGACY_FORMAT_VERSION})"
            )
        return format_version


class Subject(VaultFileModel):
    """`subjects/<slug>/subject.yaml`: the subject's name and the editor's preferences for it."""

    name: str
    style_guide: str | None = None


class Topic(VaultFileModel):
    """`subjects/<slug>/topics/<slug>/topic.yaml`: what the topic is and which sessions fed it."""

    title: str
    fidelity_mode: str = DEFAULT_FIDELITY_MODE
    created_at: datetime
    sessions: list[str] = Field(default_factory=list)
