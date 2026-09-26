"""Tolerant JSON for tool calls that arrive as text (the Claude Code backend, #320).

Through the API a tool input is always valid JSON; through the `claude` CLI it is text Claude
wrote, and a long call sometimes has a small defect. `loads_tolerant` parses what is recoverable
and raises for the rest:

- code fences and prose around the value (the first `{` or `[` starts it, anything after its end
  is ignored);
- a missing `,` between members or elements (`"a": 1 "b": 2`, `} {`);
- a trailing `,` before `}` or `]`;
- a `"` inside a string that is not escaped (`"he said "hi" to me"`);
- raw control characters (newlines, tabs) inside strings.

Each defect is fixed at the position `json` reports it, one at a time, so text inside strings is
never rewritten by a pattern. The repaired value is only JSON: callers still validate it against
their schema.
"""

from __future__ import annotations

import json
import re
from typing import Any

# One repair per reported defect; a text needing more than this is not a small defect.
MAX_REPAIRS = 64

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")
_VALUE_START = re.compile(r'["{\[\-0-9]|(?:true|false|null)\b')
_DECODER = json.JSONDecoder(strict=False)


def _strip(text: str) -> str:
    """From the first `{` or `[` on, fences and leading prose removed."""
    text = _FENCE.sub("", text.strip())
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    return text[min(starts) :] if starts else text


def _decode(text: str) -> Any:
    """The first JSON value of `text` (trailing text ignored); raises `json.JSONDecodeError`."""
    value, _ = _DECODER.raw_decode(text)
    return value


def _previous(text: str, pos: int) -> int:
    """The index of the last non-whitespace character before `pos`, or -1."""
    index = pos - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    return index


def _next(text: str, pos: int) -> str:
    """The text from the first non-whitespace character at or after `pos` on."""
    return text[pos:].lstrip()


def _repair_once(text: str, error: json.JSONDecodeError) -> str | None:
    """`text` with the defect `error` reports fixed, or `None` when it is not one we fix."""
    pos, message = error.pos, error.msg
    following = _next(text, pos)
    if message.startswith("Expecting ',' delimiter"):
        before = _previous(text, pos)
        if _VALUE_START.match(following):
            return text[:pos] + "," + text[pos:]
        if before >= 0 and text[before] == '"':
            # `"he said "hi" to me"`: the string ended at an inner quote; escape it.
            return text[:before] + '\\"' + text[before + 1 :]
        return None
    closing = following[:1] in ("}", "]")
    if message.startswith(("Expecting property name", "Expecting value")) and closing:
        comma = _previous(text, pos)
        if comma >= 0 and text[comma] == ",":
            return text[:comma] + text[comma + 1 :]
        return None
    if message.startswith("Expecting ':' delimiter"):
        before = _previous(text, pos)
        if before >= 0 and text[before] == '"':
            # A key or string value broken by an inner quote: escape it.
            return text[:before] + '\\"' + text[before + 1 :]
    return None


def loads_tolerant(text: str) -> Any:
    """`text` parsed as JSON, recoverable defects repaired (see the module docstring).

    Raises the first `json.JSONDecodeError` (the original defect) when it cannot be repaired.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        first = error
    candidate = _strip(text)
    for _ in range(MAX_REPAIRS + 1):
        try:
            return _decode(candidate)
        except json.JSONDecodeError as error:
            repaired = _repair_once(candidate, error)
            if repaired is None or repaired == candidate:
                break
            candidate = repaired
    raise first
