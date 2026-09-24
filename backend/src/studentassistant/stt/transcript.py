"""The transcript assembler: final segments in arrival order -> the session's canonical transcript.

Pure logic, no I/O. `TranscriptAssembler.add` takes each final `NormalisedSegment` as it arrives
(from the client's recognizer or a server-side provider, already in session time) and returns the
segment to append to `transcript.jsonl`, or None when it adds nothing. The rules, in order:

1. **Duplicate**: a final with the same span and text as one already written is dropped.
2. **Covered span**: a final whose span lies entirely inside an already written final with the
   same text is dropped.
3. **Restarted recognizer**: a final that overlaps a recent written final in time (one of the
   last `OVERLAP_LOOKBACK`; the last one also when the new final starts before it) and repeats
   its words is trimmed: a final whose words all lie, in a row, inside that written final is
   dropped, and otherwise the longest prefix of its words that repeats the end of the written
   final is cut (its start then moves to that final's end). The transcript never holds the
   same spoken words twice; a final left with no words is dropped.
4. **Late final**: written segments have non-decreasing starts. A final that starts before the
   last written one is placed after it: its start is clamped to the last written start (its end to
   no earlier than that). A late final carrying new words is never dropped.

Partials are never written: `add` ignores them. Words are compared case-insensitively and without
punctuation; the written text keeps the recognizer's original spelling.
"""

from __future__ import annotations

import re

from studentassistant.stt.models import NormalisedSegment

_WORD = re.compile(r"\w+")

OVERLAP_LOOKBACK = 8
"""How many of the latest written finals a new final is checked against for repeated words."""


def _key(token: str) -> str:
    """A token's comparison key: its letters and digits, lowercased (the token when it has none)."""
    return "".join(_WORD.findall(token)).casefold() or token


def _tokens(text: str) -> list[str]:
    return text.split()


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """Whether `needle` appears in `haystack` as a contiguous run."""
    size = len(needle)
    return any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


def _repeated_prefix(previous: list[str], current: list[str]) -> int:
    """How many leading words of `current` repeat the trailing words of `previous`."""
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


class TranscriptAssembler:
    """One session's transcript under construction; feed it every final in arrival order."""

    def __init__(self) -> None:
        self.written: list[NormalisedSegment] = []
        self._spans: set[tuple[float, float, str]] = set()

    @property
    def last(self) -> NormalisedSegment | None:
        return self.written[-1] if self.written else None

    def seed(self, segment: NormalisedSegment) -> None:
        """Record a segment the transcript already holds (a resumed session), without rules."""
        self.written.append(segment)
        self._spans.add((segment.start, segment.end, segment.text))

    def add(self, segment: NormalisedSegment) -> NormalisedSegment | None:
        """The segment to append for `segment` (possibly trimmed or moved), or None to drop it."""
        if not segment.is_final:
            return None
        text = segment.text.strip()
        if not text:
            return None
        if (segment.start, segment.end, text) in self._spans:
            return None
        if any(
            w.text == text and w.start <= segment.start and segment.end <= w.end
            for w in self.written
        ):
            return None
        result = self._against_last(segment.model_copy(update={"text": text}))
        if result is not None:
            self.written.append(result)
            self._spans.add((segment.start, segment.end, text))
            self._spans.add((result.start, result.end, result.text))
        return result

    def _against_last(self, segment: NormalisedSegment) -> NormalisedSegment | None:
        last = self.last
        if last is None:
            return segment
        start, end, tokens = segment.start, segment.end, _tokens(segment.text)
        keys = [_key(t) for t in tokens]
        for written in self.written[-OVERLAP_LOOKBACK:]:
            overlaps = start < written.end and end > written.start
            if not (overlaps or (written is last and start <= last.start)):
                continue
            # It overlaps a recent final (or arrived late): cut the words it repeats.
            written_keys = [_key(t) for t in _tokens(written.text)]
            if _contains_run(written_keys, keys):
                return None
            cut = _repeated_prefix(written_keys, keys)
            if cut:
                tokens, keys = tokens[cut:], keys[cut:]
                start = min(max(start, written.end), end)
        if start < last.start:
            start = last.start
        end = max(end, start)
        return segment.model_copy(update={"start": start, "end": end, "text": " ".join(tokens)})


__all__ = ["TranscriptAssembler"]
