"""The model behind every YAML file the vault writes, in the layout of `docs/modules/vault.md`.

The fields are declared in the order the file shows them, because that order is what the
deterministic dump writes and what keeps the git diffs of the vault small (ADR-0002).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORMAT_VERSION = 1
DEFAULT_FIDELITY_MODE = "estricto"


class VaultFileModel(BaseModel):
    """What every model backed by a vault file has in common."""

    # The vault is the source of truth, so a key this backend does not declare means the file was
    # written by a newer one: refusing it is better than dropping the student's content on the next
    # read-modify-write. Which layout versions are readable at all is `format_version`'s job.
    model_config = ConfigDict(extra="forbid")


class VaultMeta(VaultFileModel):
    """`vault.yaml`: which layout the vault uses, since when, and whose it is."""

    format_version: int = FORMAT_VERSION
    created_at: datetime
    student: str

    @field_validator("format_version")
    @classmethod
    def known_format_version(cls, format_version: int) -> int:
        if format_version != FORMAT_VERSION:
            raise ValueError(
                f"unsupported vault format version {format_version}"
                f" (this backend only reads and writes {FORMAT_VERSION})"
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
