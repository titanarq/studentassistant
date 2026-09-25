"""What the live observer shows Claude: the topic block, the state at the start and each batch.

Pure code (no I/O, no LLM): the live loop (`live.py`) turns bus events into `BatchItem`s with
`batch_item`, a topic's folded state into the text that opens a conversation with `render_state`,
and a batch into the text of one user turn with `render_batch`.

Scope (ADR-0003): the context is only the session's topic -- its subject and title, its digest,
its folded state -- and the events of the session being observed. Nothing here ever reads another
topic.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from studentassistant.observer.fold import (
    CAPTURE_EVENT_KIND,
    CAPTURE_ID_KEY,
    SEGMENT_EVENT_KIND,
    SEGMENT_ID_KEY,
)
from studentassistant.observer.ops import STATE_OP_EVENT_KIND
from studentassistant.observer.state import EventRef, PendingRefs, TopicState

# The event kinds the observer reads besides the two the fold registers. The page transcription
# (#50) MUST publish `PAGE_TRANSCRIPTION_KIND` with `payload.capture_id` and `payload.text`.
PAGE_TRANSCRIPTION_KIND = "page.transcribed"
BUTTON_KIND = "button"
MARKER_KIND = "marker"
COMMAND_KIND = "command"
SWITCH_SOURCE = "switch_source"
OBSERVER_ORIGIN = "observer"

BATCH_KINDS = frozenset(
    {
        SEGMENT_EVENT_KIND,
        CAPTURE_EVENT_KIND,
        PAGE_TRANSCRIPTION_KIND,
        BUTTON_KIND,
        MARKER_KIND,
        COMMAND_KIND,
        STATE_OP_EVENT_KIND,
    }
)
"""Every kind that can become part of a batch (`batch_item` decides which events do)."""

# How much of the state's free notes the opening context repeats (the most recent ones).
MAX_CONTEXT_NOTES = 20


@dataclass(frozen=True)
class BatchItem:
    """One line of a batch: what happened, and how it counts toward sending the batch.

    `segment` items count toward the batch's segment trigger and `speech_seconds` toward its speech
    trigger; an `immediate` item (a capture, a page transcription, a source switch) sends the batch
    as soon as no call is in flight. `ref` is the stored event the item shows (`None` for a
    transient one); an answered batch acknowledges the newest (`catchup.py`).
    """

    line: str
    segment: bool = False
    speech_seconds: float = 0.0
    immediate: bool = False
    ref: EventRef | None = None

    def at(self, ref: EventRef | None) -> BatchItem:
        """This item, showing the stored event `ref`."""
        return BatchItem(self.line, self.segment, self.speech_seconds, self.immediate, ref)


def _seconds(ms: Any) -> float:
    return ms / 1000 if isinstance(ms, int | float) else 0.0


def _compact(payload: Mapping[str, Any], drop: Iterable[str] = ()) -> str:
    kept = {key: value for key, value in payload.items() if key not in set(drop)}
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def batch_item(kind: str, origin: str, t: int, payload: Mapping[str, Any]) -> BatchItem | None:
    """The batch line of one bus event, or `None` for an event the observer does not show.

    The observer's own state ops are not shown (they are already in the conversation); a state
    op of any other origin is, as a `student op`.
    """
    at = f"{t / 1000:.1f} s"
    if kind == SEGMENT_EVENT_KIND:
        start = _seconds(payload.get("session_start_ms", t))
        end = max(start, _seconds(payload.get("session_end_ms", t)))
        text = str(payload.get("text", "")).strip()
        return BatchItem(
            line=f"segment {payload.get(SEGMENT_ID_KEY)} [{start:.1f}-{end:.1f} s]: {text}",
            segment=True,
            speech_seconds=end - start,
        )
    if kind == CAPTURE_EVENT_KIND:
        source = payload.get("source_context", "notes")
        trigger = payload.get("trigger", "?")
        return BatchItem(
            line=f"capture {payload.get(CAPTURE_ID_KEY)} [{at}]: source={source} trigger={trigger}",
            immediate=True,
        )
    if kind == PAGE_TRANSCRIPTION_KIND:
        text = str(payload.get("text", "")).strip()
        return BatchItem(line=f"page {payload.get(CAPTURE_ID_KEY)} [{at}]:\n{text}", immediate=True)
    if kind == BUTTON_KIND:
        button = payload.get("button")
        source = payload.get("source")
        detail = f" source={source}" if source is not None else ""
        return BatchItem(line=f"button {button} [{at}]{detail}", immediate=button == SWITCH_SOURCE)
    if kind == MARKER_KIND:
        return BatchItem(
            line=f"marker [{at}] {_compact(payload, ('client_time_ms', 'backend_time_ms'))}"
        )
    if kind == COMMAND_KIND:
        return BatchItem(line=f"command [{at}] {_compact(payload)}")
    if kind == STATE_OP_EVENT_KIND and origin != OBSERVER_ORIGIN:
        return BatchItem(line=f"student op [{at}] {_compact(payload)}")
    return None


def render_batch(items: Iterable[BatchItem], number: int) -> str:
    """The text of the user turn carrying batch `number` (1-based within the conversation)."""
    lines = "\n".join(item.line for item in items)
    return f"Batch {number}:\n{lines}"


def render_topic(subject: str, topic: str, digest: str | None) -> str:
    """The topic block of the system prompt: stable for the whole session, so it is cached."""
    text = f"Subject: {subject}\nTopic: {topic}\n"
    if digest:
        text += f"\nDigest of the earlier sessions of this topic:\n{digest.strip()}\n"
    else:
        text += "\nNo digest of earlier sessions of this topic.\n"
    return text


def _refs(refs: PendingRefs) -> str:
    parts = [
        f"{name} {', '.join(ids)}"
        for name, ids in (
            ("pages", refs.pages),
            ("segments", refs.segments),
            ("sources", refs.sources),
        )
        if ids
    ]
    return f"; {'; '.join(parts)}" if parts else ""


def render_state(
    state: TopicState,
    *,
    session_id: str,
    resumed: bool,
    rolled: bool = False,
    tail: Sequence[str] = (),
) -> str:
    """The record of the topic as the observer starts (or resumes) watching `session_id`.

    Segment texts are not repeated: the conversation is rebuilt from this state and the digest,
    never from the full history of the topic. `rolled` is a context purge (#60) in the middle of
    the session: the conversation so far was dropped, and `tail` (the batch lines of the newest
    segments already answered) is repeated so the next batch reads in context.
    """
    out: list[str] = []
    if rolled:
        out.append(
            f"Session {session_id} continues. The conversation so far was dropped to keep the"
            " context small; nothing is lost, the record below holds everything it decided."
            " The record of this topic so far:"
        )
    else:
        verb = "resumes" if resumed else "starts"
        out.append(f"Session {session_id} {verb} now. The record of this topic so far:")
    if not state.sections:
        out.append("\nOutline: empty.")
    else:
        out.append("\nOutline (section id: title, segments assigned):")

        def walk(parent: str | None, depth: int) -> None:
            for section in state.outline(parent):
                count = len(state.segments_of(section.id))
                out.append(f"{'  ' * depth}- {section.id}: {section.title} ({count})")
                walk(section.id, depth + 1)

        walk(None, 0)
    if state.concepts:
        out.append("\nConcepts (id: name [section]):")
        for concept in state.concepts.values():
            where = f" [{concept.section_id}]" if concept.section_id else ""
            out.append(f"- {concept.id}: {concept.name}{where}")
    known = list(state.segments)
    out.append(f"\nSegments stored so far: {len(known)}.")
    unassigned = [segment for segment in known if segment not in state.assignments]
    if unassigned:
        out.append("Unassigned segment ids: " + ", ".join(unassigned[-50:]))
    if state.captures:
        out.append("\nCaptures (id: linked segments):")
        for capture_id in state.captures:
            links = state.capture_links.get(capture_id, [])
            out.append(f"- {capture_id}: {', '.join(links) if links else 'not linked'}")
    if state.source_context is not None:
        reference = state.source_context.reference
        out.append(
            f"\nSource context: {state.source_context.kind}"
            + (f" ({reference})" if reference else "")
        )
    open_items = state.open_pending()
    if open_items:
        out.append("\nOpen pending items (id [kind]: text; refs):")
        for item in open_items:
            out.append(f"- {item.id} [{item.kind}]: {item.text}{_refs(item.refs)}")
    resolved = state.resolved_pending()
    if resolved:
        out.append(
            "Closed pending items: " + ", ".join(f"{item.id} ({item.status})" for item in resolved)
        )
    used = [*state.pending, *state.pending_aliases]
    if used:
        out.append("Pending ids already used (never reuse one): " + ", ".join(used))
    if state.notes:
        out.append("\nObserver notes (most recent last):")
        out.extend(f"- {note.text}" for note in state.notes[-MAX_CONTEXT_NOTES:])
    if tail:
        out.append(
            "\nThe newest segments of this session, already answered (do not handle them again):"
        )
        out.extend(tail)
    out.append("\nThe batches of this session follow.")
    return "\n".join(out)
