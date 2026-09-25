"""Vocabulary hints: the domain terms of a session a recognizer can be biased towards (#54).

Words like "sufragio censitario" are what a general-purpose recognizer gets wrong, and they are
exactly the subject's and topic's names and the concepts the observer has already extracted. This
module turns those into a short list of hints, pure logic with no I/O: the server gathers the
terms (vault metadata, observer state) and hands the result to the server-side provider
(`SpeechToTextProvider.set_vocabulary`) and to the capture client (protocol 1.4
`vocabulary_hints`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from studentassistant.config import (
    DEFAULT_STT_VOCABULARY_MAX_CHARS,
    DEFAULT_STT_VOCABULARY_MAX_TERMS,
    SttSettings,
)
from studentassistant.protocol import VOCABULARY_HINT_MAX_CHARS, VOCABULARY_HINTS_MAX_ITEMS

# What `hotwords_text` puts between two hints (also counted by the `max_chars` cap).
SEPARATOR = ", "

_SPACES = re.compile(r"\s+")


def _clean(term: str) -> str:
    return _SPACES.sub(" ", term).strip()


def _key(term: str) -> str:
    """Terms compare case- and accent-insensitively: "Límite" and "limite" are one hint."""
    decomposed = unicodedata.normalize("NFKD", term.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def vocabulary_hints(
    *,
    subject: str | None = None,
    topic: str | None = None,
    concepts: Iterable[str] = (),
    max_terms: int = DEFAULT_STT_VOCABULARY_MAX_TERMS,
    max_chars: int = DEFAULT_STT_VOCABULARY_MAX_CHARS,
) -> list[str]:
    """The session's hints, most important first, capped in count and length.

    Order: the subject's name, the topic's name, then the concepts **newest first** (`concepts`
    is given in the order they were extracted; the latest ones are what the student is talking
    about now, so they survive the cap). Whitespace is collapsed; a blank term, one longer than
    `VOCABULARY_HINT_MAX_CHARS` and a repeat (case and accents ignored) are skipped. At most
    `max_terms` hints (never more than `VOCABULARY_HINTS_MAX_ITEMS`), and the hints joined with
    `SEPARATOR` fit in `max_chars`: a term that would not fit is skipped and a shorter later one
    may still be taken. `max_terms <= 0` turns hints off (an empty list).
    """
    limit = min(max_terms, VOCABULARY_HINTS_MAX_ITEMS)
    if limit <= 0 or max_chars <= 0:
        return []
    ordered = [subject or "", topic or "", *reversed(list(concepts))]
    hints: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in ordered:
        term = _clean(raw)
        if not term or len(term) > VOCABULARY_HINT_MAX_CHARS:
            continue
        key = _key(term)
        if key in seen:
            continue
        cost = len(term) + (len(SEPARATOR) if hints else 0)
        if used + cost > max_chars:
            continue
        hints.append(term)
        seen.add(key)
        used += cost
        if len(hints) >= limit:
            break
    return hints


def vocabulary_hints_from_settings(
    settings: SttSettings,
    *,
    subject: str | None = None,
    topic: str | None = None,
    concepts: Iterable[str] = (),
) -> list[str]:
    """`vocabulary_hints` capped by `settings.vocabulary_max_terms` / `vocabulary_max_chars`."""
    return vocabulary_hints(
        subject=subject,
        topic=topic,
        concepts=concepts,
        max_terms=settings.vocabulary_max_terms,
        max_chars=settings.vocabulary_max_chars,
    )


def hotwords_text(hints: Sequence[str]) -> str | None:
    """The hints as one phrase list for a model prompt (Whisper `hotwords`); `None` when empty."""
    return SEPARATOR.join(hints) if hints else None


__all__ = [
    "SEPARATOR",
    "hotwords_text",
    "vocabulary_hints",
    "vocabulary_hints_from_settings",
]
