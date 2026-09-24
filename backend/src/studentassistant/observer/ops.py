"""The observer's state ops: the only way its knowledge of a topic changes (ADR-0003).

The observer never edits a mutable state blob. It emits ops, each one stored as the `payload` of
one session event of kind `STATE_OP_EVENT_KIND`, and the state is the fold of those events
(`studentassistant.observer.fold`). An op is a Pydantic model whose `op` field names it, so a
payload parses into the right model through the discriminated union `StateOp` (`parse_op`).

Pure code: no file system, no LLM. Whether an op's ids exist is not checked here, because that
depends on the state it is applied to; `studentassistant.observer.fold.validate_op` does it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from studentassistant.vault.sources import SourceKind

# The event kind whose payload is one state op. Named once here; every reader and writer of
# observer state ops uses this constant (documented in docs/modules/observer.md).
STATE_OP_EVENT_KIND = "observer.state_op"

# An id the observer gives to something it creates (a section, a concept, a pending item), or the
# id another module gave to a segment or a capture.
Id = Annotated[str, Field(min_length=1, max_length=200)]

PendingCategory = Literal[
    "illegible",
    "unexplained_concept",
    "incomplete",
    "possible_error",
    "contradiction",
]


class _Op(BaseModel):
    # An op is part of the event contract: a key it does not declare is a mistake of whoever
    # emitted it (a model inventing a field), not something to drop silently.
    model_config = ConfigDict(extra="forbid", frozen=True)


class AddSection(_Op):
    """A new section of the outline, at the end of its parent's children (top level if none)."""

    op: Literal["add_section"] = "add_section"
    section_id: Id
    title: str = Field(min_length=1)
    parent_id: Id | None = None


class RenameSection(_Op):
    """A new title for an existing section."""

    op: Literal["rename_section"] = "rename_section"
    section_id: Id
    title: str = Field(min_length=1)


class AssignSegments(_Op):
    """Transcript segments belong to a section; a segment assigned before moves to this one."""

    op: Literal["assign_segments"] = "assign_segments"
    section_id: Id
    segment_ids: list[Id] = Field(min_length=1)


class AddConcept(_Op):
    """A concept the student mentioned, optionally under a section and tied to segments."""

    op: Literal["add_concept"] = "add_concept"
    concept_id: Id
    name: str = Field(min_length=1)
    section_id: Id | None = None
    segment_ids: list[Id] = Field(default_factory=list)


class LinkCapture(_Op):
    """A stored capture (a page photo) goes with these transcript segments; links accumulate."""

    op: Literal["link_capture"] = "link_capture"
    capture_id: Id
    segment_ids: list[Id] = Field(min_length=1)


class SetSourceContext(_Op):
    """What the student is working from now: their notes, a book, a PDF or a web page."""

    op: Literal["set_source_context"] = "set_source_context"
    kind: SourceKind
    reference: str | None = None


class AddPending(_Op):
    """An item the student has to review later (illegible word, unexplained concept, ...)."""

    op: Literal["add_pending"] = "add_pending"
    pending_id: Id
    category: PendingCategory
    description: str = Field(min_length=1)
    segment_ids: list[Id] = Field(default_factory=list)
    capture_ids: list[Id] = Field(default_factory=list)


class ResolvePending(_Op):
    """An open pending item is settled, with how it was settled."""

    op: Literal["resolve_pending"] = "resolve_pending"
    pending_id: Id
    resolution: str = Field(min_length=1)


class Note(_Op):
    """A free remark of the observer, kept in the state for the digest and the editor."""

    op: Literal["note"] = "note"
    text: str = Field(min_length=1)
    segment_ids: list[Id] = Field(default_factory=list)


StateOp = Annotated[
    AddSection
    | RenameSection
    | AssignSegments
    | AddConcept
    | LinkCapture
    | SetSourceContext
    | AddPending
    | ResolvePending
    | Note,
    Field(discriminator="op"),
]

STATE_OP_ADAPTER: TypeAdapter[StateOp] = TypeAdapter(StateOp)

OP_NAMES: tuple[str, ...] = (
    "add_section",
    "rename_section",
    "assign_segments",
    "add_concept",
    "link_capture",
    "set_source_context",
    "add_pending",
    "resolve_pending",
    "note",
)


def parse_op(payload: dict[str, Any]) -> StateOp:
    """The op a `STATE_OP_EVENT_KIND` payload holds.

    Raises:
        pydantic.ValidationError: when the payload names no known op or does not fit its model.
    """
    return STATE_OP_ADAPTER.validate_python(payload)


def op_payload(op: StateOp) -> dict[str, Any]:
    """The event payload that stores `op`, the inverse of `parse_op`."""
    return op.model_dump(mode="json")
