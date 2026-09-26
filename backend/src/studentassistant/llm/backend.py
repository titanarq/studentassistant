"""Which way Claude is reached on this PC: the Anthropic API or the headless Claude Code CLI.

`[llm] backend` is `api`, `claude-code` or `auto` (the default). `auto` resolves to `api` when an
API key is on this machine -- `ANTHROPIC_API_KEY`, the key file `setup` stores
(`llm.api_key_file`) or an `ant auth` profile -- and to `claude-code` otherwise, so a PC with only
a Claude subscription works without configuring anything. Either way every call still goes
through `studentassistant.llm` (ADR-0004).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from studentassistant.config import Settings
from studentassistant.llm.credentials import find_ant_profile
from studentassistant.llm.transport import AnthropicTransport, Transport

ResolvedBackend = Literal["api", "claude-code"]

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"


def _key_file_has_key(path: Path) -> bool:
    """Whether the `KEY=value` file `path` holds a non-empty `ANTHROPIC_API_KEY` (never kept)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == API_KEY_ENV_VAR and value.strip().strip("\"'"):
            return True
    return False


def api_key_available(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    ant_profile: Callable[[], str | None] = find_ant_profile,
) -> bool:
    """Whether the Anthropic SDK would find a key on this PC (the environment, the key file,
    or an `ant auth` profile). Nothing secret is returned or logged."""
    environ = os.environ if environ is None else environ
    if environ.get(API_KEY_ENV_VAR):
        return True
    if _key_file_has_key(settings.llm.api_key_path()):
        return True
    return ant_profile() is not None


def resolve_backend(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    ant_profile: Callable[[], str | None] = find_ant_profile,
) -> ResolvedBackend:
    """`[llm] backend`, with `auto` turned into `api` (a key is available) or `claude-code`."""
    settings = settings or Settings()
    backend = settings.llm.backend
    if backend != "auto":
        return backend
    if api_key_available(settings, environ=environ, ant_profile=ant_profile):
        return "api"
    return "claude-code"


def default_transport(settings: Settings | None = None) -> Transport:
    """The real transport of the resolved backend. One instance should serve the whole process
    (the server passes it to every feature), so Claude Code conversations are reused."""
    settings = settings or Settings()
    if resolve_backend(settings) == "claude-code":
        from studentassistant.llm.claude_code import ClaudeCodeTransport

        return ClaudeCodeTransport(settings.llm.claude_code)
    return AnthropicTransport()
