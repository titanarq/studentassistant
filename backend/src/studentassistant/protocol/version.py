"""`protocol_version` (MAJOR.MINOR) and its negotiation.

Peers with the same MAJOR interoperate and speak the lower of the two MINORs; a different MAJOR is
refused with a message naming both versions.
"""

from __future__ import annotations

import re

PROTOCOL_VERSION = "1.0"

VERSION_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
_VERSION_RE = re.compile(VERSION_PATTERN)


class IncompatibleProtocolVersionError(ValueError):
    """The peer speaks a protocol MAJOR version this side does not."""

    def __init__(self, peer: str, ours: str = PROTOCOL_VERSION) -> None:
        self.peer = peer
        self.ours = ours
        super().__init__(
            f"incompatible protocol_version {peer}: this side speaks {ours}; "
            f"update the older side so both share MAJOR version {parse_version(ours)[0]}"
        )


def parse_version(version: str) -> tuple[int, int]:
    """Split `MAJOR.MINOR` into integers; anything else is a `ValueError`."""
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"malformed protocol_version {version!r}: expected MAJOR.MINOR")
    return int(match[1]), int(match[2])


def check_compatible(peer: str, ours: str = PROTOCOL_VERSION) -> None:
    """Raise `IncompatibleProtocolVersionError` unless `peer` shares our MAJOR version."""
    if parse_version(peer)[0] != parse_version(ours)[0]:
        raise IncompatibleProtocolVersionError(peer, ours)


def negotiate(peer: str, ours: str = PROTOCOL_VERSION) -> str:
    """The version both sides speak: the shared MAJOR with the lower MINOR."""
    check_compatible(peer, ours)
    return min(peer, ours, key=parse_version)
