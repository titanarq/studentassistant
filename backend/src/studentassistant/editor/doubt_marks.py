"""Where the open doubts are marked in the notes viewer (#516); reads only, no Claude call.

Doubts never go into the notes (#325). Instead of asking them one after another in the chat, the
web marks each open doubt next to what it is about, from what the vault already knows:

- **Blocks**: the blocks of `apuntes.md` that cite the doubt's sources -- its captures' pages and
  its source refs, compared by file (`reviewed.source_key`), as `reviewed.blocks_citing` does --
  and, for a transcript segment (or a transcript span among its refs), the blocks whose transcript
  citation (`sessions/<id>#t=HH:MM:SS-HH:MM:SS`) shares a stretch of time with it in the same
  session; a segment whose time is unknown matches any citation of its session. Headings and
  footnote definitions are never marked.
- **Section**: a doubt no block matches goes on the heading of its section -- the observer's
  section of its transcript segments (or that section's parent), matched to exactly one heading of
  the notes by title (numbering, case, accents and punctuation aside).
- **Top**: with no clear section, in a header at the top of the notes.

Only the doubts the chat would have asked count (`doubts.ask_plan`'s rules): open ones whose refs
overlap the sources the notes cite, or that have no refs, never one whose pages are all set aside
by capture triage; a doubt asked in the chat and still open counts too.

Blocks are addressed as the web viewer counts them (the "¿Por qué?" numbering): per section (its
heading's anchor; `None` before the first section), from 1 after the heading -- before the first
section from the start of the notes, the `# title` included --, footnote definitions left out.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field

from studentassistant.editor.doubts import (
    _cited_keys,
    _item_page_keys,
    _read_all,
    _state,
    _Strict,
)
from studentassistant.editor.inputs import capture_pages, segment_times
from studentassistant.editor.notes_format import (
    Block,
    NotesDocument,
    ProvenanceError,
    parse,
    parse_provenance,
)
from studentassistant.editor.reviewed import source_key
from studentassistant.observer import PendingItem, TopicState
from studentassistant.sources import set_aside_ids
from studentassistant.vault import Vault, read_notes

_SPAN = re.compile(
    r"^sessions/([^/#]+)(?:/[^#]*)?#t=(\d{2}):(\d{2}):(\d{2})-(\d{2}):(\d{2}):(\d{2})$"
)
_NUMBERING = re.compile(r"^\s*(?:\d+|[ivxlc]+)(?:\.\d+)*[.)]?\s+", re.IGNORECASE)

Span = tuple[str, int, int]
"""A transcript span: `(session_id, start, end)` in whole seconds."""


class MarkedBlock(_Strict):
    """A block of the notes, as the web viewer numbers it (module docstring)."""

    section: str | None = Field(description="The anchor of its section; None before the first.")
    number: int = Field(ge=1, description="Its number in that section, from 1.")


class DoubtMark(_Strict):
    """Where one open doubt is marked: `blocks`, else the heading of `section`, else the top."""

    pending_id: str
    kind: str
    text: str
    level: Literal["block", "section", "top"]
    blocks: list[MarkedBlock] = Field(default_factory=list)
    section: str | None = None
    asked: bool = Field(default=False, description="Asked in the chat and not answered yet.")


class DoubtMarks(_Strict):
    """Every marked open doubt of a topic, in the order of the notes (the top ones first)."""

    subject: str
    topic: str
    count: int
    marks: list[DoubtMark] = Field(default_factory=list)


def _span(ref: str) -> Span | None:
    match = _SPAN.match(ref)
    if match is None:
        return None
    h1, m1, s1, h2, m2, s2 = (int(g) for g in match.groups()[1:])
    return match.group(1), h1 * 3600 + m1 * 60 + s1, h2 * 3600 + m2 * 60 + s2


def _overlaps(a: Span, b: Span) -> bool:
    """Same session and a shared stretch of time (spans that only touch do not count, unless one
    is a single instant)."""
    if a[0] != b[0]:
        return False
    low, high = max(a[1], b[1]), min(a[2], b[2])
    return low < high or (low == high and (a[1] == a[2] or b[1] == b[2]))


def _normal(title: str) -> str:
    text = unicodedata.normalize("NFKD", _NUMBERING.sub("", title))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


class _Located:
    """The notes' markable blocks with what each cites, and the order of everything."""

    def __init__(self, document: NotesDocument) -> None:
        definitions = {d.label: d for d in document.footnotes}
        self.blocks: list[tuple[MarkedBlock, set[str], list[Span], int]] = []
        """(address, source files cited, transcript spans cited, position)."""
        self.headings: dict[str, int] = {}
        """Section anchor -> position of its heading."""
        self.titles: dict[str, list[str]] = {}
        """Normalised heading title -> the anchors that carry it."""
        position = 0
        groups: list[tuple[str | None, list[Block]]] = [(None, document.preamble)]
        for section in document.sections:
            groups.append((section.anchor, section.blocks))
        for index, (anchor, blocks) in enumerate(groups):
            if index > 0:
                position += 1
                if anchor is not None:
                    heading = document.sections[index - 1].heading
                    self.headings.setdefault(anchor, position)
                    self.titles.setdefault(_normal(heading.title), []).append(anchor)
            number = 0
            for block in blocks:
                if block.kind == "footnotes":
                    continue
                number += 1
                position += 1
                if block.kind in ("title", "rule"):
                    continue
                files: set[str] = set()
                spans: list[Span] = []
                for label in block.footnote_refs:
                    definition = definitions.get(label)
                    if definition is None:
                        continue
                    try:
                        provenance = parse_provenance(definition)
                    except ProvenanceError:
                        continue
                    if provenance.source_id is None:
                        continue
                    files.add(source_key(provenance.source_id))
                    span = _span(provenance.source_id)
                    if span is not None:
                        spans.append(span)
                address = MarkedBlock(section=anchor, number=number)
                self.blocks.append((address, files, spans, position))


def _item_targets(
    item: PendingItem,
    pages: dict[str, str],
    state: TopicState,
    times: dict[str, Span],
) -> tuple[set[str], list[Span], set[str]]:
    """What a doubt is about: source files matched whole, transcript spans matched by overlap,
    and sessions matched whole (a segment whose time is unknown)."""
    files = _item_page_keys(item, pages)
    spans: list[Span] = []
    sessions: set[str] = set()
    for ref in item.refs.sources:
        if not ref.startswith("sessions/"):
            continue
        span = _span(ref)
        if span is not None:
            spans.append(span)
        else:
            sessions.add(source_key(ref))
    for segment in item.refs.segments:
        if segment in times:
            spans.append(times[segment])
        elif segment in state.segments:
            sessions.add(f"sessions/{state.segments[segment].session_id}")
    return files, spans, sessions


def _item_section(item: PendingItem, state: TopicState, located: _Located) -> str | None:
    """The one notes heading the observer's section of the doubt's segments is titled as."""
    found: set[str] = set()
    for segment in item.refs.segments:
        section_id = state.assignments.get(segment)
        while section_id is not None and section_id in state.sections:
            section = state.sections[section_id]
            anchors = located.titles.get(_normal(section.title), [])
            if len(anchors) == 1:
                found.add(anchors[0])
                break
            section_id = section.parent_id
    return found.pop() if len(found) == 1 else None


def doubt_marks(vault: Vault, subject_slug: str, topic_slug: str) -> DoubtMarks:
    """Where each open doubt is marked in the notes viewer (module docstring); reads only
    (blocking). Without notes nothing is marked.

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: the vault's errors for an unknown topic.
        ObserverStateError: the topic's events cannot be folded.
    """
    notes = read_notes(vault, subject_slug, topic_slug)
    if not notes or not notes.strip():
        return DoubtMarks(subject=subject_slug, topic=topic_slug, count=0)
    state = _state(vault, subject_slug, topic_slug)
    records = _read_all(vault, subject_slug, topic_slug)
    pages = capture_pages(vault, subject_slug, topic_slug)
    set_aside = set_aside_ids(vault, subject_slug, topic_slug)
    times = segment_times(vault, subject_slug, topic_slug)
    cited = _cited_keys(notes)
    located = _Located(parse(notes))
    in_chat = {question.pending_id for question in records.chat}
    ordered: list[tuple[int, int, DoubtMark]] = []
    for order, item in enumerate(state.open_pending()):
        page_keys = _item_page_keys(item, pages)
        if page_keys and all(key in set_aside for key in page_keys):
            continue
        asked = any(pending_id in in_chat for pending_id in (item.id, *item.merged_ids))
        keys = page_keys | {
            f"sessions/{state.segments[segment].session_id}"
            for segment in item.refs.segments
            if segment in state.segments
        }
        if not asked and keys and not keys & cited:
            continue
        files, spans, sessions = _item_targets(item, pages, state, times)
        hits = [
            (address, position)
            for address, block_files, block_spans, position in located.blocks
            if block_files & (files | sessions)
            or any(_overlaps(a, b) for a in spans for b in block_spans)
        ]
        base = {"pending_id": item.id, "kind": item.kind, "text": item.text, "asked": asked}
        if hits:
            mark = DoubtMark(**base, level="block", blocks=[address for address, _ in hits])
            position = hits[0][1]
        elif (anchor := _item_section(item, state, located)) is not None:
            mark = DoubtMark(**base, level="section", section=anchor)
            position = located.headings[anchor]
        else:
            mark = DoubtMark(**base, level="top")
            position = -1
        ordered.append((position, order, mark))
    marks = [mark for _, _, mark in sorted(ordered, key=lambda entry: entry[:2])]
    return DoubtMarks(subject=subject_slug, topic=topic_slug, count=len(marks), marks=marks)


__all__ = ["DoubtMark", "DoubtMarks", "MarkedBlock", "doubt_marks"]
