"""The users of a shared vault and the active user of a request (protocol 1.8, #544).

Several students share one backend and one vault, each with their own `users/<id>/` folder. The
bodies below are what `GET /api/users`, `POST /api/users` and `PATCH /api/users/{user_id}`
exchange; the profile photo has no JSON body (`PUT /api/users/{user_id}/photo` takes the raw
image of one of `USER_PHOTO_CONTENT_TYPES` and answers with the updated `User`).

Which user any other route acts for comes from the request itself: the `USER_HEADER` header, else
the `USER_COOKIE` cookie (a present header wins), else -- when the vault holds exactly one user --
that user, else a `400 user_required` refusal; an unknown id is `404 user_not_found`. The
WebSocket handshake follows the same rule. This selects a user, it does not authenticate one: the
bearer/loopback trust of ADR-0001 is unchanged and a device pairs with the backend, not with a
user.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from studentassistant.protocol.base import Id, ProtocolModel

USER_HEADER = "X-SA-User"
"""Request header naming the user a request acts for (the native Android app)."""

USER_COOKIE = "sa_user"
"""Cookie naming the same user (the web page and the Android WebView)."""

USER_NAME_MAX_CHARS = 80
"""The longest `User.name`."""

USER_EMAIL_MAX_CHARS = 254
"""The longest `User.email` (RFC 5321's limit for an address)."""

USER_PHOTO_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")
"""The image types `PUT /api/users/{user_id}/photo` accepts; `GET` answers `image/jpeg`."""

# The name travels trimmed: no whitespace at either end and at least one character that is not
# whitespace, so a blank name is impossible.
USER_NAME_PATTERN = r"^\S(?:.*\S)?$"
USER_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
USER_PHOTO_URL_PATTERN = r"^/api/users/"

UserName = Annotated[
    str, Field(min_length=1, max_length=USER_NAME_MAX_CHARS, pattern=USER_NAME_PATTERN)
]
UserEmail = Annotated[str, Field(max_length=USER_EMAIL_MAX_CHARS, pattern=USER_EMAIL_PATTERN)]
UserPhotoUrl = Annotated[str, Field(pattern=USER_PHOTO_URL_PATTERN)]


class User(ProtocolModel):
    """A student of this vault: the body of the create and update responses, and an item of the
    list response.

    `email` and `photo_url` are absent, never `null`, when the user has none.
    """

    id: Id
    name: UserName
    email: UserEmail | None = None
    photo_url: UserPhotoUrl | None = None


class UsersListResponse(ProtocolModel):
    users: list[User]


class UserCreateRequest(ProtocolModel):
    name: UserName
    email: UserEmail | None = None


class UserUpdateRequest(ProtocolModel):
    """A profile change: at least one of the two fields, an absent one left as it is.

    `email` as the empty string clears it (and the answered `User` then has no `email`); a new
    email follows the same rules as a created one.
    """

    name: UserName | None = None
    email: UserEmail | Literal[""] | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> UserUpdateRequest:
        if self.name is None and self.email is None:
            raise ValueError("a user update carries at least one of `name` or `email`")
        return self
