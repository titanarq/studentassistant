"""The observer's knowledge of one topic: what `fold` computes from the topic's session events.

Pure models, no file system. Every collection is keyed or ordered by the order the events that
built it were folded in, so two folds of the same events are equal field by field and dump to the
same JSON (the snapshot, `studentassistant.observer.snapshot`, relies on that).

Each item remembers the event it came from as an `EventRef` (`session_id`, `seq`), because `seq`
restarts in every session: that pair is what lets the editor answer "why is this here?".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.observer.ops import Id, PendingKind
from studentassistant.vault import Origin, SourceKind

PendingStatus = Literal["open", "auto_resolved", "resolved", "dismissed"]

# Bumped whenever the fold's meaning changes: a snapshot of another version is folded again from
# scratch rather than trusted, and a compacted log written by a newer version is refused.
STATE_VERSION = 2
# 2 (#55): pending items carry kind/text/refs/created_by/status, duplicates merge (aliases).


class _StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventRef(_StateModel):
    """One event of the topic: `seq` is per session, so the session is part of the reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    seq: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        """The sort key of the event: sessions in id order, then `seq` within a session."""
        return (self.session_id, self.seq)


class Section(_StateModel):
    """One section of the outline; `parent_id` is `None` for a top-level one."""

    id: Id
    title: str
    parent_id: Id | None = None
    added_at: EventRef


class Concept(_StateModel):
    id: Id
    name: str
    section_id: Id | None = None
    segment_ids: list[Id] = Field(default_factory=list)
    added_at: EventRef


class SourceContext(_StateModel):
    """What the student was working from since `set_at`."""

    kind: SourceKind
    reference: str | None = None
    set_at: EventRef


class PendingRefs(_StateModel):
    """What a pending item is about: page captures, transcript segments and sources."""

    pages: list[Id] = Field(default_factory=list)
    segments: list[Id] = Field(default_factory=list)
    sources: list[Id] = Field(default_factory=list)

    def overlaps(self, other: PendingRefs) -> bool:
        """Whether the two share a page, a segment or a source."""
        return bool(
            set(self.pages) & set(other.pages)
            or set(self.segments) & set(other.segments)
            or set(self.sources) & set(other.sources)
        )


class PendingItem(_StateModel):
    """A pending-review item (a doubt the student will settle later, never interrupted for).

    `kind` and `text` (Spanish) say what the doubt is, `refs` what it is about, `created_by` the
    origin of the event that added it. `status` is `open` until a `resolve_pending` closes it
    (`resolution`, `resolved_at`). `merged_ids` are the ids of later `add_pending` ops the fold
    found to be this same doubt and merged into it (their refs joined this item's).
    """

    id: Id
    kind: PendingKind
    text: str
    refs: PendingRefs = Field(default_factory=PendingRefs)
    created_by: Origin
    status: PendingStatus = "open"
    resolution: str | None = None
    added_at: EventRef
    resolved_at: EventRef | None = None
    merged_ids: list[Id] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class ObserverNote(_StateModel):
    text: str
    segment_ids: list[Id] = Field(default_factory=list)
    added_at: EventRef


class TopicState(_StateModel):
    """The observer's knowledge of a topic, folded from the events of all its sessions.

    - `sections`: the outline, by id, in the order the sections were added.
    - `segments` / `captures`: every transcript segment and capture the fold has seen stored, by
      id, with the event that stored it; ops may only reference these.
    - `assignments`: segment id -> section id (the last assignment wins).
    - `concepts`: by id, in the order they were added.
    - `capture_links`: capture id -> the segment ids it goes with, in the order they were linked.
    - `source_context`: the current one, `None` until the first `set_source_context`.
    - `pending`: every pending item, open and closed, by id, in the order they were added.
    - `pending_aliases`: the id of an `add_pending` merged into another item -> that item's id.
    - `notes`: the observer's free remarks, in order.
    """

    sections: dict[Id, Section] = Field(default_factory=dict)
    segments: dict[Id, EventRef] = Field(default_factory=dict)
    captures: dict[Id, EventRef] = Field(default_factory=dict)
    assignments: dict[Id, Id] = Field(default_factory=dict)
    concepts: dict[Id, Concept] = Field(default_factory=dict)
    capture_links: dict[Id, list[Id]] = Field(default_factory=dict)
    source_context: SourceContext | None = None
    pending: dict[Id, PendingItem] = Field(default_factory=dict)
    pending_aliases: dict[Id, Id] = Field(default_factory=dict)
    notes: list[ObserverNote] = Field(default_factory=list)

    def outline(self, parent_id: str | None = None) -> list[Section]:
        """The sections directly under `parent_id` (top level for `None`), in the order added."""
        return [s for s in self.sections.values() if s.parent_id == parent_id]

    def open_pending(self) -> list[PendingItem]:
        return [item for item in self.pending.values() if item.is_open]

    def resolved_pending(self) -> list[PendingItem]:
        """The closed items: resolved, auto-resolved or dismissed."""
        return [item for item in self.pending.values() if not item.is_open]

    def pending_item(self, pending_id: str) -> PendingItem | None:
        """The item `pending_id` names, following a merged id to the item it was merged into."""
        return self.pending.get(self.pending_aliases.get(pending_id, pending_id))

    def segments_of(self, section_id: str) -> list[str]:
        """The segments assigned to `section_id`, in the order the segments were stored."""
        return [seg for seg in self.segments if self.assignments.get(seg) == section_id]
