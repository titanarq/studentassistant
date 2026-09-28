"""Mermaid diagrams in the printed exports (#481): the code block plus a Spanish note.

The web draws a ```` ```mermaid ```` fence as a diagram (#480), but the exam PDF (PyMuPDF's
`Story`) and the Marp PDF/PPTX cannot: rendering mermaid needs its JavaScript library and a
browser, neither of which the backend has at export time. So an export keeps the diagram's code as
a code block and adds `DIAGRAM_UNAVAILABLE` after it; the Markdown files keep the fence as it is,
so the web preview still draws it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

DIAGRAM_UNAVAILABLE = "Diagrama no disponible en la exportación"

_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n`]*)$")


@dataclass(frozen=True)
class Fence:
    """A fenced code block of a text's lines: its language, its body lines (without the fence's
    own indentation), that indentation, and where it is (`end` is one past its last line)."""

    lang: str
    body_lines: tuple[str, ...]
    indent: str
    start: int
    end: int
    closed: bool

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines)

    @property
    def is_mermaid(self) -> bool:
        return self.lang.lower() == "mermaid"


def _closes(line: str, fence: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def fences(lines: list[str]) -> Iterator[Fence]:
    """The fenced code blocks of `lines`, in order; an unclosed fence runs to the end."""
    index = 0
    while index < len(lines):
        match = _OPEN.match(lines[index])
        if match is None:
            index += 1
            continue
        fence = match.group("fence")
        close = index + 1
        while close < len(lines) and not _closes(lines[close], fence):
            close += 1
        indent = match.group("indent")
        yield Fence(
            lang=(match.group("info").split() or [""])[0],
            body_lines=tuple(_dedent(line, len(indent)) for line in lines[index + 1 : close]),
            indent=indent,
            start=index,
            end=min(close + 1, len(lines)),
            closed=close < len(lines),
        )
        index = close + 1


def _dedent(line: str, width: int) -> str:
    """`line` without up to `width` leading spaces (the fence's own indentation)."""
    strip = len(line) - len(line.lstrip(" \t"))
    return line[min(strip, width) :]


def note_mermaid_fences(markdown: str) -> str:
    """`markdown` with `*Diagrama no disponible en la exportación*` after every mermaid fence,
    at the fence's indentation (so it stays inside a list item holding the fence)."""
    lines = markdown.split("\n")
    out: list[str] = []
    done = 0
    for fence in fences(lines):
        if not fence.is_mermaid or not fence.closed:
            continue
        out += lines[done : fence.end]
        out.append(f"{fence.indent}*{DIAGRAM_UNAVAILABLE}*")
        if fence.end < len(lines) and lines[fence.end].strip():
            out.append("")  # its own paragraph, not the start of the next one
        done = fence.end
    out += lines[done:]
    return "\n".join(out)
