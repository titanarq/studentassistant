"""The models of a session's files: `session.yaml` and one line of each of its two JSONL logs.

Pure code, no file system. `SessionMeta` is a vault YAML file like `topic.yaml`, so it is as strict
as one (`extra="forbid"`). `Event` and `TranscriptSegment` are lines of append-only logs whose
schemas are part of the contract and versioned (ADR-0003): a reader must accept a line an older
backend wrote and a `kind` it has never heard of, so they validate only the envelope and leave the
`payload` to whoever understands the `kind`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.vault.models import VaultFileModel

# The schema version the event envelope written by this backend has; readers accept older ones.
EVENT_SCHEMA_VERSION = 1

SESSION_ID_PATTERN = r"^\d{8}-\d{6}$"
SESSION_ID_FORMAT = "%Y%m%d-%H%M%S"

Origin = Literal["phone", "stt", "observer", "editor", "user"]


class SessionMeta(VaultFileModel):
    """`sessions/<session-id>/session.yaml`: when the session ran, on which host, which protocol."""

    id: str = Field(pattern=SESSION_ID_PATTERN)
    started_at: datetime
    ended_at: datetime | None = None
    host: str
    protocol_version: str


class Event(BaseModel):
    """One line of `events.jsonl`: the envelope of ADR-0003 around a `kind`-specific payload.

    `seq` is assigned by the store and `t` is milliseconds since the session's `started_at`.
    `kind` is any non-empty string, because the event kinds grow with the product and a reader
    that refused an unknown one could not replay an older or a newer session.
    """

    # A field a newer envelope adds is ignored on read rather than refused: the log is never
    # rewritten from what was read, so nothing is lost by not keeping it.
    model_config = ConfigDict(extra="ignore")

    seq: int = Field(ge=1)
    t: int = Field(ge=0)
    origin: Origin
    kind: str = Field(min_length=1)
    schema_version: int = Field(default=EVENT_SCHEMA_VERSION, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class TranscriptWord(BaseModel):
    """One word of a segment with its own timing, when the STT provider gives them."""

    model_config = ConfigDict(extra="ignore")

    text: str
    t_start: int = Field(ge=0)
    t_end: int = Field(ge=0)
    confidence: float | None = None


class TranscriptSegment(BaseModel):
    """One line of `transcript.jsonl`: a final segment, its span in session milliseconds."""

    model_config = ConfigDict(extra="ignore")

    seq: int = Field(ge=1)
    t_start: int = Field(ge=0)
    t_end: int = Field(ge=0)
    text: str
    words: list[TranscriptWord] | None = None
