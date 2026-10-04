"""Which user a request acts for, and that user's own vault handle (protocol 1.8, #544, #549).

One backend and one vault serve several students, each with their own `users/<id>/` folder
(`docs/modules/vault.md`), and nothing but the request itself says who is calling: the `X-SA-User`
header (what the native Android app sends), else the `sa_user` cookie (what the web page and the
Android WebView set), else -- when the vault holds exactly one user -- that one, so a client that
knows nothing about users keeps working on a single-user vault. `resolve_user_id` is that rule and
nothing else; `active_user_vault` is the FastAPI dependency that turns it into the two handles a
user-scoped route works on, `vault.for_user(id)` and `sync.for_user(id)`, both views of what
`SessionService.open_vault()` has open. A WebSocket handshake follows the same rule, on the header
and the cookie of the upgrade request (`resolve_websocket_user`).

**The selection is not authentication.** Neither the header nor the cookie is a credential: they
name a user, they do not prove one, and whoever can reach the backend can name any user of it --
the bearer/loopback trust of ADR-0001 is what guards the door, unchanged, and a device pairs with
the backend, never with a user. That is also why neither value is a secret: `server/redaction.py`
keeps tokens and pairing codes out of the logs, and a user id is neither, so the logs still say
which student a request was about.

The two refusals are the coded ones of `server.errors` -- `400 user_required` when no user can be
assumed, `404 user_not_found` for an id this vault does not have -- and they are the same for
every caller version: a client older than 1.8 gets the Spanish `detail` and reads the status, as
`protocol/README.md` "Users (1.8)" wants.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from starlette.websockets import WebSocket

from studentassistant.protocol import USER_COOKIE, USER_HEADER
from studentassistant.server.errors import unknown_user_error, user_required_error
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import GitSync, UserGitSync, UserNotFoundError, Vault, user_ids

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."


class UserRequiredError(Exception):
    """The request names no user and this vault holds none or more than one, so none is assumed."""


class UnknownUserError(Exception):
    """The user the request names is not one of this vault's."""

    def __init__(self, user_id: str) -> None:
        super().__init__(f"this vault has no user {user_id!r}")
        self.user_id = user_id


def resolve_user_id(headers: Mapping[str, str], cookies: Mapping[str, str], vault: Vault) -> str:
    """The id of the user this request acts for, by the rule of "Users (1.8)".

    `headers` and `cookies` are the ones of the request or of the WebSocket handshake. A present
    header wins over the cookie, and a value that is there but blank names nobody, so the cookie
    still gets its turn. With neither, a vault holding exactly one user acts for that one; with
    neither and any other number there is nothing to assume.

    A folder under `users/` counts as a user whether or not its `profile.json` can be read back --
    `vault.user_ids`' own rule, and the cheap one: one listing of the folder, no JSON read per
    request. Whether a handle can be opened on it is the next question, and the one
    `active_user_vault` answers.

    This reads the file system, so a caller on the event loop runs it in a worker thread.

    Raises:
        UserRequiredError: no user is named and this vault has none, or more than one.
        UnknownUserError: the named user is not one of this vault's.
    """
    ids = user_ids(vault)
    named = _named_user(headers, cookies)
    if named is None:
        if len(ids) == 1:
            return ids[0]
        raise UserRequiredError(
            f"the request names no user and the vault at {vault.root} holds {len(ids)}"
        )
    if named not in ids:
        raise UnknownUserError(named)
    return named


def resolve_websocket_user(websocket: WebSocket, vault: Vault) -> str:
    """The active user of a WebSocket handshake: the same rule, on the upgrade request.

    A handshake carries no REST body, so the two refusals travel as the errors they are and the
    gateway decides how to close on them (#550).

    Raises:
        UserRequiredError, UnknownUserError: as `resolve_user_id`.
    """
    return resolve_user_id(websocket.headers, websocket.cookies, vault)


async def active_user_vault(request: Request) -> tuple[Vault, UserGitSync]:
    """The active user's `(user_vault, user_sync)`: the dependency of every user-scoped route.

    Both are views of what the session service has open -- the user's content folder, and that
    folder's paths and notes tags in the one repository's git sync -- so a route keeps calling the
    readers and writers it already calls and nothing it writes can land in another user's folder.
    The vault work runs in a worker thread; the event loop only waits for it.

    Raises:
        HTTPException: 503 when the vault cannot be opened at all, and the two coded refusals of
            `resolve_user_id` -- `400 user_required`, `404 user_not_found`.
    """
    sessions: SessionService = request.app.state.sessions
    try:
        vault = await sessions.open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
        ) from error
    sync = sessions.sync
    assert sync is not None, "open_vault() has just made the sync the app was not given"
    headers = dict(request.headers)
    cookies = dict(request.cookies)
    try:
        return await asyncio.to_thread(_user_scope, vault, sync, headers, cookies)
    except UserRequiredError as error:
        raise user_required_error() from error
    except UnknownUserError as error:
        raise unknown_user_error(error.user_id) from error


UserScope = Annotated[tuple[Vault, UserGitSync], Depends(active_user_vault)]
"""How a user-scoped route declares the dependency: `scope: UserScope` in its signature.

The route then works on `scope[0]` (the user's content handle, whose `user_id` is the student it
acts for) and `scope[1]` (that folder's view of the repository's git sync). A route that only needs
to tell the `SessionService` which student a call is for reads `scope[0].user_id` and nothing else,
because the service narrows the vault itself.
"""


def _user_scope(
    vault: Vault, sync: GitSync, headers: Mapping[str, str], cookies: Mapping[str, str]
) -> tuple[Vault, UserGitSync]:
    """The user the request names and both of their handles, in one blocking step."""
    user_id = resolve_user_id(headers, cookies, vault)
    try:
        return vault.for_user(user_id), sync.for_user(user_id)
    except UserNotFoundError as error:
        # A folder with no `profile.json` to open a handle on: `resolve_user_id` counts it as a
        # user, and what the student is told is that this id is not one they can work as.
        raise UnknownUserError(user_id) from error


def _named_user(headers: Mapping[str, str], cookies: Mapping[str, str]) -> str | None:
    """The user the request names: the header when it says something, else the cookie, else None.

    Header names are matched case-insensitively because that is how they travel, whatever the
    mapping a caller hands over has done with them.
    """
    wanted = USER_HEADER.lower()
    for name, value in headers.items():
        if name.lower() == wanted and value.strip():
            return value.strip()
    return cookies.get(USER_COOKIE, "").strip() or None
