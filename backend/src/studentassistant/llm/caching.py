"""Prompt-caching helpers: the stable prefix (tools, system, topic sources) carries the breakpoint.

The API renders `tools` -> `system` -> `messages` and caches by prefix, so one `cache_control`
marker on the last stable block caches everything before it; volatile content goes after it.
"""

from __future__ import annotations

import copy
from typing import Any

EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def system_blocks(system: str | list[str] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalise a system prompt (a string, strings or text blocks) to a list of text blocks."""
    if system is None:
        return []
    if isinstance(system, str):
        return [{"type": "text", "text": system}] if system else []
    blocks: list[dict[str, Any]] = []
    for part in system:
        blocks.append({"type": "text", "text": part} if isinstance(part, str) else dict(part))
    return blocks


def cache_stable_prefix(
    system: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copies of `system` and `tools` with `cache_control` on the last block of the stable prefix.

    The breakpoint goes on the last system block (which also covers the tools rendered before it);
    with no system prompt it goes on the last tool. Blocks that already carry `cache_control`
    are left as the caller set them.
    """
    system = copy.deepcopy(system)
    tools = copy.deepcopy(tools)
    target = system[-1] if system else (tools[-1] if tools else None)
    if target is not None and "cache_control" not in target:
        target["cache_control"] = dict(EPHEMERAL)
    return system, tools


def cached_block(text: str) -> dict[str, Any]:
    """A text block marked as a cache breakpoint, e.g. the topic sources ending a stable prefix."""
    return {"type": "text", "text": text, "cache_control": dict(EPHEMERAL)}
