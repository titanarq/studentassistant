"""The pending-review queue: deduplication of doubts and the `review/pending.yaml` it becomes.

The observer accumulates doubts without interrupting the student (VISION §5 item 7): an
illegible word, a concept named but not explained, incomplete information, a possible error, a
contradiction between sources. Each is an `add_pending` op; the same doubt noticed twice (the word
is still unreadable on the next photo of the page, the concept is named again) must stay one item.

`find_duplicate(state, kind, text, refs)` is that rule, applied by the fold to every `add_pending`
(so replaying a log always merges the same way): an open item of the same `kind` is the same
doubt when

- its text is very similar (`STRONG_SIMILARITY`), or
- the two share a page, a segment or a source and their texts are somewhat similar
  (`OVERLAP_SIMILARITY`): two different illegible words on the same page stay two items.

Text similarity compares the texts without case, accents or punctuation (`text_similarity`).
Texts that name different numbers (page 3 and page 4, formula 1 and formula 2) are never the same
doubt, however alike the rest is.
Closed items are never merged into: a doubt that comes back after it was settled is a new one.

`pending_review(state)` is the content of `review/pending.yaml` (the open count and every item,
open ones first), which the live loop and the loader regenerate from the fold through
`studentassistant.vault.write_pending_review`, and which the web reads through the API.

Pure code: no file system, no LLM.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.observer.state import PendingItem, PendingRefs, TopicState

STRONG_SIMILARITY = 0.85
"""Texts at least this similar are the same doubt of a kind, whatever they refer to."""

OVERLAP_SIMILARITY = 0.5
"""Texts at least this similar are the same doubt of a kind when their refs overlap."""

REVIEW_FORMAT_VERSION = 1

_WORD = re.compile(r"[a-z0-9]+")
_NUMBER = re.compile(r"\d+")


def normalize_text(text: str) -> str:
    """`text` lowercased, without accents, as its words joined by single spaces."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_WORD.findall(plain))


def text_similarity(a: str, b: str) -> float:
    """How alike two texts are, from 0 to 1: the better of a character and a word comparison.

    The character ratio catches the same sentence with small changes; the word overlap (Jaccard
    over the word sets) catches the same words in another order.
    """
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return 1.0 if left == right else 0.0
    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    words_left, words_right = set(left.split()), set(right.split())
    jaccard = len(words_left & words_right) / len(words_left | words_right)
    return max(ratio, jaccard)


def is_duplicate(item: PendingItem, kind: str, text: str, refs: PendingRefs) -> bool:
    """Whether a new doubt (`kind`, `text`, `refs`) is the open `item` noticed again."""
    if not item.is_open or item.kind != kind:
        return False
    numbers, new_numbers = set(_NUMBER.findall(item.text)), set(_NUMBER.findall(text))
    if numbers and new_numbers and numbers != new_numbers:
        return False
    similarity = text_similarity(item.text, text)
    if similarity >= STRONG_SIMILARITY:
        return True
    return similarity >= OVERLAP_SIMILARITY and item.refs.overlaps(refs)


def find_duplicate(state: TopicState, kind: str, text: str, refs: PendingRefs) -> str | None:
    """The id of the open item of `state` a new doubt duplicates, `None` when it is a new one.

    When several match, the one added first wins, so a merge is deterministic.
    """
    for item in state.pending.values():
        if is_duplicate(item, kind, text, refs):
            return item.id
    return None


class PendingReview(BaseModel):
    """`review/pending.yaml`: the topic's pending-review queue, regenerated from the fold.

    `open_count` is what the phone's counter shows; `items` holds every item, the open ones first,
    each group in the order the items were added.
    """

    model_config = ConfigDict(extra="forbid")

    format_version: int = REVIEW_FORMAT_VERSION
    open_count: int = Field(ge=0)
    items: list[PendingItem] = Field(default_factory=list)


def pending_review(state: TopicState) -> PendingReview:
    """The pending-review queue of `state`, as `review/pending.yaml` holds it."""
    open_items = state.open_pending()
    return PendingReview(open_count=len(open_items), items=[*open_items, *state.resolved_pending()])


__all__ = [
    "OVERLAP_SIMILARITY",
    "REVIEW_FORMAT_VERSION",
    "STRONG_SIMILARITY",
    "PendingReview",
    "find_duplicate",
    "is_duplicate",
    "normalize_text",
    "pending_review",
    "text_similarity",
]
