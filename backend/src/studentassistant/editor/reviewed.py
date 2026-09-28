"""Which blocks of the notes the student already reviewed, and which of them are settled (#474).

There is no per-block review state in the vault: the review is derived from append-only
`notes.reviewed` records in `conversations/editor.jsonl` (the existing conversation API, no vault
layout change). Each record carries a `reason` -- `student_edit` (the student saved the notes:
the blocks they added or changed) or `doubt_closed` (the student answered or dismissed a doubt:
the blocks then citing its sources) -- and the `blocks` it covers as keys (`block_key`: the
SHA-256 of the block's text without footnote references, whitespace collapsed). Editing a block
changes its key, so it is no longer reviewed until it is reviewed again; moving a block or
changing only its footnotes keeps it.

A block is **settled** (`settled_blocks`) when it is reviewed, has no `[[?` mark and no open doubt
names a source it cites (a doubt's captures' pages, its source refs, and the sessions of its
transcript segments; a doubt whose pages are all set aside by triage is never asked, so it does
not count). The editor's block map marks settled blocks «[revisado]».
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Literal

from studentassistant.editor.inputs import capture_pages
from studentassistant.editor.notes_format import (
    Block,
    NotesDocument,
    ProvenanceError,
    has_doubt_mark,
    parse,
    parse_provenance,
)
from studentassistant.observer import PendingItem, TopicState, load_observer_snapshot
from studentassistant.sources import set_aside_ids
from studentassistant.vault import (
    ConversationRecord,
    Vault,
    append_conversation_record,
    read_conversation,
)

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "editor"
NOTES_REVIEWED_RECORD = "notes.reviewed"
ReviewReason = Literal["student_edit", "doubt_closed"]
Clock = Callable[[], datetime]

_REFERENCE = re.compile(r"\[\^[^\]\s]+\](?!:)")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def block_key(text: str) -> str:
    """The key of a block: SHA-256 (hex) of its text without footnote refs, whitespace collapsed."""
    bare = " ".join(_REFERENCE.sub("", text).split())
    return hashlib.sha256(bare.encode("utf-8")).hexdigest()


def content_blocks(document: NotesDocument) -> list[Block]:
    """The blocks a review can cover: every block but the footnote definitions, in order."""
    blocks = [*document.preamble, *(b for s in document.sections for b in s.blocks)]
    return [block for block in blocks if block.kind != "footnotes"]


def source_key(ref: str) -> str:
    """The file a source id or reference names: `sources/pdf/x.pdf#page=3` -> `sources/pdf/x.pdf`,
    `sessions/<id>#t=...` or `sessions/<id>/transcript.jsonl` -> `sessions/<id>`."""
    path = ref.split("#", 1)[0]
    if path.startswith("sessions/"):
        return "/".join(path.split("/")[:2])
    return path


def block_sources(document: NotesDocument, block: Block) -> set[str]:
    """The source files (`source_key`) the footnotes a block cites point to."""
    definitions = {d.label: d for d in document.footnotes}
    keys: set[str] = set()
    for label in block.footnote_refs:
        definition = definitions.get(label)
        if definition is None:
            continue
        try:
            provenance = parse_provenance(definition)
        except ProvenanceError:
            continue
        if provenance.source_id is not None:
            keys.add(source_key(provenance.source_id))
    return keys


def changed_block_keys(before: str | None, after: str) -> list[str]:
    """The keys of the blocks of `after` whose key no block of `before` has (added or changed)."""
    old = {block_key(b.text) for b in content_blocks(parse(before or ""))}
    keys = [block_key(b.text) for b in content_blocks(parse(after))]
    return list(dict.fromkeys(key for key in keys if key not in old))


def blocks_citing(notes: str | None, sources: Iterable[str]) -> list[str]:
    """The keys of the blocks of `notes` that cite any of `sources` (compared by `source_key`)."""
    wanted = {source_key(ref) for ref in sources}
    if not wanted or not notes:
        return []
    document = parse(notes)
    keys = [
        block_key(block.text)
        for block in content_blocks(document)
        if block_sources(document, block) & wanted
    ]
    return list(dict.fromkeys(keys))


def item_sources(item: PendingItem, pages: dict[str, str], state: TopicState) -> set[str]:
    """The source files a doubt names: its captures' pages, its source refs and the sessions of
    its transcript segments."""
    keys = {source_key(pages[capture]) for capture in item.refs.pages if capture in pages}
    keys |= {source_key(ref) for ref in item.refs.sources}
    keys |= {
        f"sessions/{state.segments[segment].session_id}"
        for segment in item.refs.segments
        if segment in state.segments
    }
    return keys


def record_reviewed(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    reason: ReviewReason,
    blocks: Iterable[str],
    *,
    clock: Clock = _utc_now,
) -> bool:
    """Append one `notes.reviewed` record; blocking. Nothing is written without blocks.

    Returns whether a record was written. A failure to write is only logged: losing a review
    record never fails what the student did.
    """
    keys = list(dict.fromkeys(blocks))
    if not keys:
        return False
    entry = ConversationRecord(
        time=clock(), kind=NOTES_REVIEWED_RECORD, detail={"reason": reason, "blocks": keys}
    )
    try:
        append_conversation_record(vault, subject_slug, topic_slug, CONVERSATION_NAME, entry)
    except Exception:
        logger.exception("could not record the reviewed blocks of %s/%s", subject_slug, topic_slug)
        return False
    return True


def reviewed_keys(vault: Vault, subject_slug: str, topic_slug: str) -> set[str]:
    """Every block key a `notes.reviewed` record of the topic covers; blocking."""
    keys: set[str] = set()
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION_NAME):
        if record.kind != NOTES_REVIEWED_RECORD or not record.detail:
            continue
        blocks = record.detail.get("blocks")
        if isinstance(blocks, list):
            keys.update(key for key in blocks if isinstance(key, str))
    return keys


def open_doubt_sources(vault: Vault, subject_slug: str, topic_slug: str) -> set[str]:
    """The source files the topic's open doubts name (those set aside by triage excepted)."""
    state = load_observer_snapshot(vault, subject_slug, topic_slug, write_back=False).state
    pages = capture_pages(vault, subject_slug, topic_slug)
    set_aside = set_aside_ids(vault, subject_slug, topic_slug)
    keys: set[str] = set()
    for item in state.open_pending():
        page_keys = {
            source_key(pages[capture]) for capture in item.refs.pages if capture in pages
        } | {source_key(ref) for ref in item.refs.sources if not ref.startswith("sessions/")}
        if page_keys and all(key in set_aside for key in page_keys):
            continue
        keys |= item_sources(item, pages, state)
    return keys


def settled_blocks(vault: Vault, subject_slug: str, topic_slug: str, notes: str | None) -> set[str]:
    """The keys of the settled blocks of `notes`: reviewed, with no `[[?` mark and citing no
    source an open doubt names; blocking."""
    if not notes or not notes.strip():
        return set()
    reviewed = reviewed_keys(vault, subject_slug, topic_slug)
    if not reviewed:
        return set()
    doubted = open_doubt_sources(vault, subject_slug, topic_slug)
    document = parse(notes)
    settled: set[str] = set()
    for block in content_blocks(document):
        key = block_key(block.text)
        if key not in reviewed or has_doubt_mark(block.text):
            continue
        if block_sources(document, block) & doubted:
            continue
        settled.add(key)
    return settled


__all__ = [
    "NOTES_REVIEWED_RECORD",
    "ReviewReason",
    "block_key",
    "block_sources",
    "blocks_citing",
    "changed_block_keys",
    "content_blocks",
    "item_sources",
    "open_doubt_sources",
    "record_reviewed",
    "reviewed_keys",
    "settled_blocks",
    "source_key",
]
