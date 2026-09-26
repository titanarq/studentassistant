"""Machine-readable codes of REST error bodies (protocol 1.2).

A REST error body is `{"detail": "<Spanish sentence>"}`; for the refusals a client branches on it
also carries `code`, one of `ErrorCode`, so the client never matches the Spanish wording. `code`
is optional and additive: it is sent only to a client whose negotiated version is at least
`ERROR_CODE_SINCE`, and a client must treat an unknown code like no code.
"""

from __future__ import annotations

from enum import StrEnum

ERROR_CODE_SINCE = (1, 2)
"""The protocol version that added `code` to REST error bodies."""


class ErrorCode(StrEnum):
    """The stable `code` of a REST error body."""

    COST_CAP_REACHED = "cost_cap_reached"
    """409: the session's or the day's cost cap is reached; retry with `confirm_over_cap`."""

    DOUBT_CLOSED = "doubt_closed"
    """409: the doubt was already answered, auto-resolved or dismissed."""

    SESSION_OPEN = "session_open"
    """409: an unended session is in the way (another session when starting one, or the
    topic's own session when resolving its doubts)."""

    NOTES_CHANGED = "notes_changed"
    """409: the topic's notes changed since the `base_revision` a student save started from; the
    body also carries the current notes' `text` and `revision`."""

    NOTES_BUSY = "notes_busy"
    """409: "prepárame el tema", a restore or another rewrite holds the topic's notes."""
