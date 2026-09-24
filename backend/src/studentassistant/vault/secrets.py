"""The secret guard: nothing that looks like an API key or a token is ever written to the vault.

The vault is pushed to GitHub (ADR-0002), so a key that reaches it is a key that leaves the PC, and
git history keeps it after the file is fixed. Every vault writer (`files.py` for whole files,
`jsonl.py` for appended lines) runs `guard` on the exact bytes it is about to write, before it
opens anything, so a refused write leaves the disk as it was.

The patterns are the prefixes and the body shapes the common providers document, with a minimum
body length, so ordinary text that merely mentions a prefix ("sk-ant-" in a sentence about keys)
is not mistaken for a key. The error names the pattern and never the matched text: an error
message ends up in logs and in the web UI, and echoing the key there would leak it anyway.
"""

from __future__ import annotations

import re

from studentassistant.vault.errors import VaultError

# name -> pattern. Word boundaries keep a rule off the middle of a longer identifier; they are
# ASCII ones, so a Latin-1 byte next to a key in a binary file still counts as a boundary.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-api-key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}", re.ASCII),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}", re.ASCII),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}", re.ASCII),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", re.ASCII),
    "private-key-block": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----", re.ASCII),
}


class SecretRefused(VaultError):  # noqa: N818 -- the name issue #20 gives it
    """A write refused because its content matches a secret pattern; the message names which."""

    def __init__(self, pattern: str) -> None:
        super().__init__(
            f"refused to write to the vault: the content matches the {pattern!r} secret pattern"
            " (API keys and tokens never enter the vault)"
        )
        self.pattern = pattern


def looks_like_secret(content: bytes | str) -> str | None:
    """Return the name of the first secret pattern `content` matches, or `None` when none does.

    Bytes are read as Latin-1, which maps every byte to one character, so a binary file (a page
    image) is scanned as it is without a decoding error and ASCII keys inside it still match.
    """
    text = content.decode("latin-1") if isinstance(content, bytes) else content
    for name, pattern in _PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def guard(content: bytes | str) -> None:
    """Refuse `content` when it looks like a secret.

    Raises:
        SecretRefused: naming the pattern that matched, never the matched text.
    """
    pattern = looks_like_secret(content)
    if pattern is not None:
        raise SecretRefused(pattern)
