"""Machine-readable codes of REST error bodies (protocol 1.2).

A REST error body is `{"detail": "<Spanish sentence>"}`; for the refusals a client branches on it
also carries `code`, one of `ErrorCode`, so the client never matches the Spanish wording. `code`
is optional and additive: a code is sent only to a client whose negotiated version is at least
that code's own (`ERROR_CODES_SINCE`), and a client must treat an unknown code like no code.
"""

from __future__ import annotations

from enum import StrEnum

ERROR_CODE_SINCE = (1, 2)
"""The protocol version that added `code` to REST error bodies, and the version of every code
below except the two user ones."""

USER_ERROR_CODES_SINCE = (1, 8)
"""The protocol version that added `user_required` and `user_not_found` (#545)."""


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

    USER_REQUIRED = "user_required"
    """400: the request does not say which user it acts for (`X-SA-User` / `sa_user`) and the
    vault holds more than one, so none can be assumed."""

    USER_NOT_FOUND = "user_not_found"
    """404: this vault has no user with the id the request named."""


ERROR_CODES_SINCE: dict[ErrorCode, tuple[int, int]] = {
    ErrorCode.COST_CAP_REACHED: ERROR_CODE_SINCE,
    ErrorCode.DOUBT_CLOSED: ERROR_CODE_SINCE,
    ErrorCode.SESSION_OPEN: ERROR_CODE_SINCE,
    ErrorCode.NOTES_CHANGED: ERROR_CODE_SINCE,
    ErrorCode.NOTES_BUSY: ERROR_CODE_SINCE,
    ErrorCode.USER_REQUIRED: USER_ERROR_CODES_SINCE,
    ErrorCode.USER_NOT_FOUND: USER_ERROR_CODES_SINCE,
}
"""The protocol version each code was added in; a client negotiating an older one never sees it."""
