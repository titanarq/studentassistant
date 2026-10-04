"""`server.user_scope`: the active user of a request, over HTTP and over a WebSocket handshake.

The rule is `protocol/README.md` "Users (1.8)"'s, and every branch of it is walked twice: once
through a REST route that depends on `active_user_vault`, so a refusal is seen as the coded body a
client reads, and once through a WebSocket handshake, so the helper a gateway calls (#550) is the
one under test. Both routes are this file's own -- the real ones are the later stages of #549 and
#550-#551 -- but the app around them is `create_app`'s, with its middlewares, its error handler
and its session service, over a real vault.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import LAN_HOST, LOCAL_BASE_URL, LOOPBACK_HOST, PUBLIC_URL, HostedTestClient
from studentassistant.config import ServerSettings
from studentassistant.protocol import USER_COOKIE, USER_HEADER
from studentassistant.server.app import create_app
from studentassistant.server.auth import WS_POLICY_VIOLATION, authenticate_websocket
from studentassistant.server.errors import USER_REQUIRED_DETAIL
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.redaction import redact
from studentassistant.server.sessions import SessionService
from studentassistant.server.user_scope import (
    UnknownUserError,
    UserRequiredError,
    active_user_vault,
    resolve_user_id,
    resolve_websocket_user,
)
from studentassistant.vault import UserGitSync, Vault, create_user, user_ids

FIRST_USER = "ana-garcia"
"""The user `tests/conftest.py`'s `tmp_vault` is born with (`slugify(STUDENT)`, #548)."""

CLASSMATE = "lucia-fernandez"
NOBODY = "nadie"
REST_PATH = "/api/test/scope"
WS_PATH = "/ws/test/scope"
BEARER_DETAIL = "a valid bearer token is required"


def cookie(user_id: str) -> str:
    """A `Cookie` header carrying the `sa_user` selection."""
    return f"{USER_COOKIE}={user_id}"


def hosted_client(app: FastAPI, host: str) -> TestClient:
    """A TestClient over `app` whose requests come from `host` (as conftest's `client_at`)."""
    base_url = LOCAL_BASE_URL if host == LOOPBACK_HOST else PUBLIC_URL
    return HostedTestClient(app, base_url=base_url, client=(host, 50000))


@pytest.fixture
def two_users(tmp_vault: Vault) -> Vault:
    """`tmp_vault` with a classmate added: a vault a request has to choose between."""
    assert user_ids(tmp_vault) == [FIRST_USER]
    assert create_user(tmp_vault, "Lucía Fernández").id == CLASSMATE
    assert user_ids(tmp_vault) == [FIRST_USER, CLASSMATE]
    return tmp_vault


@pytest.fixture
def scope_app(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path
) -> Callable[[Vault], FastAPI]:
    """An app over the vault it is given, with one REST route and one WebSocket on the rule.

    `GET /api/test/scope` answers with what `active_user_vault` resolved and with proof that both
    handles are the user's own; `WS /ws/test/scope` answers with what `resolve_websocket_user`
    resolved, and closes a handshake it cannot scope with `WS_POLICY_VIOLATION`.
    """

    def make(vault: Vault) -> FastAPI:
        app = create_app(
            static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=vault
        )

        @app.get(REST_PATH)
        async def rest_scope(
            handles: Annotated[tuple[Vault, UserGitSync], Depends(active_user_vault)],
        ) -> dict[str, Any]:
            user_vault, user_sync = handles
            return {
                "user_id": user_vault.user_id,
                "path": user_vault.path.relative_to(user_vault.root).as_posix(),
                "prefix": user_sync.prefix,
            }

        @app.websocket(WS_PATH)
        async def ws_scope(websocket: WebSocket) -> None:
            if (await authenticate_websocket(websocket)) is None:
                return
            sessions: SessionService = websocket.app.state.sessions
            opened = await sessions.open_vault()
            try:
                user_id = resolve_websocket_user(websocket, opened)
            except (UserRequiredError, UnknownUserError):
                await websocket.close(code=WS_POLICY_VIOLATION)
                return
            await websocket.accept()
            await websocket.send_json({"user_id": user_id})
            await websocket.close()

        return app

    return make


# The selections that name a user, as the wire carries them: the same cases go to both routes.
NAMED_CASES = [
    pytest.param({USER_HEADER: FIRST_USER}, FIRST_USER, id="header"),
    pytest.param({USER_HEADER: CLASSMATE}, CLASSMATE, id="header-of-the-other-user"),
    pytest.param({USER_HEADER.lower(): CLASSMATE}, CLASSMATE, id="header-in-lowercase"),
    pytest.param({"Cookie": cookie(CLASSMATE)}, CLASSMATE, id="cookie"),
    pytest.param(
        {"Cookie": f"pref=dark; {cookie(CLASSMATE)}"}, CLASSMATE, id="cookie-among-others"
    ),
    pytest.param(
        {USER_HEADER: FIRST_USER, "Cookie": cookie(CLASSMATE)},
        FIRST_USER,
        id="a-present-header-wins",
    ),
]


@pytest.mark.parametrize(("headers", "expected"), NAMED_CASES)
def test_a_rest_request_acts_for_the_user_it_names(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault, headers: dict[str, str], expected: str
) -> None:
    client = hosted_client(scope_app(two_users), LOOPBACK_HOST)
    response = client.get(REST_PATH, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "user_id": expected,
        "path": f"users/{expected}",
        "prefix": f"users/{expected}/",
    }


@pytest.mark.parametrize(("headers", "expected"), NAMED_CASES)
def test_a_handshake_acts_for_the_user_it_names(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault, headers: dict[str, str], expected: str
) -> None:
    client = hosted_client(scope_app(two_users), LOOPBACK_HOST)
    with client.websocket_connect(WS_PATH, headers=dict(headers)) as socket:
        assert socket.receive_json() == {"user_id": expected}


def test_a_single_user_needs_no_selection(
    scope_app: Callable[[Vault], FastAPI], tmp_vault: Vault
) -> None:
    client = hosted_client(scope_app(tmp_vault), LOOPBACK_HOST)
    assert client.get(REST_PATH).json() == {
        "user_id": FIRST_USER,
        "path": f"users/{FIRST_USER}",
        "prefix": f"users/{FIRST_USER}/",
    }
    with client.websocket_connect(WS_PATH) as socket:
        assert socket.receive_json() == {"user_id": FIRST_USER}


def test_two_users_and_no_selection_is_400_user_required(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    response = hosted_client(scope_app(two_users), LOOPBACK_HOST).get(REST_PATH)
    assert response.status_code == 400
    assert response.json() == {"detail": USER_REQUIRED_DETAIL, "code": "user_required"}


def test_a_handshake_with_no_selection_is_closed_when_there_is_a_choice(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    client = hosted_client(scope_app(two_users), LOOPBACK_HOST)
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(WS_PATH):
        pass
    assert refused.value.code == WS_POLICY_VIOLATION


def test_a_vault_with_no_user_cannot_assume_one(
    scope_app: Callable[[Vault], FastAPI], vault_with_no_users: Vault
) -> None:
    client = hosted_client(scope_app(vault_with_no_users), LOOPBACK_HOST)
    response = client.get(REST_PATH)
    assert response.status_code == 400
    assert response.json()["code"] == "user_required"
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(WS_PATH):
        pass
    assert refused.value.code == WS_POLICY_VIOLATION


def test_an_unknown_user_is_404_user_not_found(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    response = hosted_client(scope_app(two_users), LOOPBACK_HOST).get(
        REST_PATH, headers={USER_HEADER: NOBODY}
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": f"El usuario «{NOBODY}» no existe en esta bóveda.",
        "code": "user_not_found",
    }


def test_a_handshake_that_names_an_unknown_user_is_closed(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    client = hosted_client(scope_app(two_users), LOOPBACK_HOST)
    for headers in ({USER_HEADER: NOBODY}, {"Cookie": cookie(NOBODY)}):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(WS_PATH, headers=headers),
        ):
            pass
        assert refused.value.code == WS_POLICY_VIOLATION


def test_the_user_header_is_not_a_credential(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    """A LAN client that names a user but carries no token is refused by the bearer check: the
    refusal is the authentication one, not a user one, because the selection proves nothing."""
    response = hosted_client(scope_app(two_users), LAN_HOST).get(
        REST_PATH, headers={USER_HEADER: FIRST_USER}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}


def test_a_route_that_is_not_user_scoped_is_unaffected_by_the_selection(
    scope_app: Callable[[Vault], FastAPI], two_users: Vault
) -> None:
    """Naming a user this vault does not have changes nothing on a route that does not ask.

    The route is `GET /api/users`, which is about the users themselves rather than one user's
    content and so works on the vault's ROOT handle and reads no selection (`server/user_routes.py`
    says why: a stale or bogus selection must not stop the selection screen listing the users to
    choose from). `/api/subjects*` and `/api/sessions*` cannot carry this assertion any more: #550
    hands them to the `SessionService`, which refuses a two-user vault nobody chose for.
    """
    client = hosted_client(scope_app(two_users), LOOPBACK_HOST)
    plain = client.get("/api/users")
    named = client.get("/api/users", headers={USER_HEADER: NOBODY})
    assert plain.status_code == 200
    assert [user["id"] for user in plain.json()["users"]] == [FIRST_USER, CLASSMATE]
    assert (named.status_code, named.json()) == (plain.status_code, plain.json())


def test_the_selection_is_not_a_secret_to_redact() -> None:
    """A user id is not a token: the logs keep it, and still redact the cookie it travels with."""
    header = f"{USER_HEADER}: {FIRST_USER}"
    assert redact(header) == header
    cookies = "Cookie: sa_token=sa_0123456789abcdefgh; " + cookie(FIRST_USER)
    assert redact(cookies) == "Cookie: sa_token=[REDACTED]; " + cookie(FIRST_USER)


# -- the rule on its own, on the mappings a caller hands it -------------------------------------


@pytest.mark.parametrize(
    ("headers", "cookies"),
    [
        ({"X-SA-USER": CLASSMATE}, {}),
        ({USER_HEADER: ""}, {USER_COOKIE: CLASSMATE}),
        ({USER_HEADER: "   "}, {USER_COOKIE: CLASSMATE}),
        ({USER_HEADER: f" {CLASSMATE} "}, {}),
        ({}, {USER_COOKIE: f" {CLASSMATE} "}),
        ({}, {"pref": "dark", USER_COOKIE: CLASSMATE}),
    ],
    ids=[
        "the-header-name-is-case-insensitive",
        "an-empty-header-names-nobody",
        "a-blank-header-names-nobody",
        "the-header-is-trimmed",
        "the-cookie-is-trimmed",
        "the-cookie-among-others",
    ],
)
def test_a_value_that_names_nobody_falls_through_to_the_next_one(
    two_users: Vault, headers: dict[str, str], cookies: dict[str, str]
) -> None:
    assert resolve_user_id(headers, cookies, two_users) == CLASSMATE


def test_nothing_is_assumed_when_there_is_a_choice(two_users: Vault) -> None:
    with pytest.raises(UserRequiredError):
        resolve_user_id({}, {}, two_users)


def test_nothing_is_assumed_when_there_is_nobody(vault_with_no_users: Vault) -> None:
    with pytest.raises(UserRequiredError):
        resolve_user_id({}, {}, vault_with_no_users)


@pytest.mark.parametrize("named", [NOBODY, "../ana-garcia", "..", "Ana García", f"{FIRST_USER}2"])
def test_an_id_that_is_not_a_user_of_this_vault(two_users: Vault, named: str) -> None:
    """A name, a path that climbs out of `users/` and a near miss are all simply not a user."""
    with pytest.raises(UnknownUserError) as refused:
        resolve_user_id({USER_HEADER: named}, {}, two_users)
    assert refused.value.user_id == named
