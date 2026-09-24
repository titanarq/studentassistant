"""Base model and field types shared by every protocol message."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ProtocolModel(BaseModel):
    """Strict and immutable: an unknown field is a contract violation, not something to ignore."""

    model_config = ConfigDict(extra="forbid", frozen=True)


ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Opaque backend-assigned identifier (subject, topic, session, device).
Id = Annotated[str, Field(pattern=ID_PATTERN)]
# Unix epoch milliseconds.
EpochMs = Annotated[int, Field(ge=0)]
