"""Bearer-token authentication for REST and WebSocket (ADR-0001).

`BearerAuthMiddleware` guards every HTTP route except `EXEMPT_ROUTES`: it wants
`Authorization: Bearer <token>` with a token of a paired device, and answers 401 otherwise. The
same token is also taken from the `sa_token` cookie (`TOKEN_COOKIE`), which is how the phone app's
WebView carries it to the web UI (#83): a page cannot put a header on its own loads. A
loopback client passes without a token while `server.trust_localhost` is true, so the web UI works
on the PC itself. WebSocket routes call `authenticate_websocket` before accepting.

Whoever passed is left in `scope["state"]["principal"]` (`request.state.principal`).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from studentassistant.config import ServerSettings
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.devices import DeviceStore
from studentassistant.server.network import (
    allowed_host_names,
    client_host,
    host_name,
    is_allowed_host,
    is_loopback,
    send_json,
)

EXEMPT_ROUTES = frozenset(
    {
        ("GET", "/api/health"),
        ("POST", "/api/pair"),
        ("POST", "/api/pair/codes"),
    }
)
"""The only (method, path) pairs reachable without a token."""

WS_POLICY_VIOLATION = 1008

TOKEN_COOKIE = "sa_token"
"""The cookie a paired device's token may come in when no bearer header does (#83)."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that change nothing: a cookie-only request with another method must prove its origin."""


@dataclass(frozen=True)
class Principal:
    """Who is calling: a paired device (`device_id`), or the PC itself (`local`, no token).

    `protocol_version` is the version the device sent when it paired (REST requests carry none),
    and this backend's own for the PC, whose web UI is served by this very build.
    """

    device_id: str | None
    local: bool = False
    protocol_version: str = PROTOCOL_VERSION


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _cookie(cookie_header: str | None, name: str = TOKEN_COOKIE) -> str | None:
    """The value of cookie `name` in a `Cookie` header, or None when it is absent or empty."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name and value.strip():
            return value.strip()
    return None


def authenticate(
    host: str | None,
    token: str | None,
    server: ServerSettings,
    devices: DeviceStore,
) -> Principal | None:
    """The caller behind `token` from `host`, or None when it must be refused."""
    if token:
        device = devices.verify_token(token)
        if device is not None:
            return Principal(device_id=device.id, protocol_version=device.protocol_version)
    if server.trust_localhost and is_loopback(host):
        return Principal(device_id=None, local=True)
    return None


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def same_site_origin(
    origin: str | None, referer: str | None, host_header: str | None, server: ServerSettings
) -> bool:
    """True when a cookie-authenticated request comes from this backend's own pages (#83).

    The `Origin` header (or, without one, the `Referer`) must name a host this backend is known by
    (`network.allowed_host_names` or a loopback/private literal) and the same host as the
    request's `Host`. A missing, `null` or unparsable origin fails: the cookie is ambient, so a
    page of another site could otherwise make the browser send it (CSRF / cross-site WebSocket).
    """
    source = origin if origin else referer
    if not source or source.strip().lower() == "null":
        return False
    try:
        name = urlsplit(source.strip()).hostname
    except ValueError:
        return False
    if not name:
        return False
    target = host_name(host_header)
    return (
        target is not None
        and host_name(name) == target
        and is_allowed_host(name, allowed_host_names(server))
    )


class BearerAuthMiddleware:
    """401 for every non-exempt HTTP request without a valid bearer token.

    A request authenticated only by the `sa_token` cookie with a method other than GET/HEAD/OPTIONS
    is 403 unless `same_site_origin` holds.
    """

    def __init__(self, app: ASGIApp, server: ServerSettings, devices: DeviceStore) -> None:
        self.app = app
        self.server = server
        self.devices = devices

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (scope["method"], scope["path"]) in EXEMPT_ROUTES:
            await self.app(scope, receive, send)
            return
        token = _bearer(_header(scope, b"authorization"))
        by_cookie = token is None
        if by_cookie:
            token = _cookie(_header(scope, b"cookie"))
        principal = authenticate(client_host(scope), token, self.server, self.devices)
        if principal is None:
            await send_json(
                send,
                401,
                b'{"detail":"a valid bearer token is required"}',
                [(b"www-authenticate", b"Bearer")],
            )
            return
        if (
            by_cookie
            and principal.device_id is not None
            and scope["method"] not in SAFE_METHODS
            and not same_site_origin(
                _header(scope, b"origin"),
                _header(scope, b"referer"),
                _header(scope, b"host"),
                self.server,
            )
        ):
            await send_json(
                send, 403, b'{"detail":"a cross-site request cannot use the session cookie"}'
            )
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


async def authenticate_websocket(websocket: WebSocket) -> Principal | None:
    """Check a WebSocket before it is accepted; on failure close it with 1008 and return None.

    The token comes from `Authorization: Bearer <token>` or, since browsers cannot set headers on
    a WebSocket, from the `?token=` query parameter or the `sa_token` cookie. A handshake
    authenticated only by the cookie must also pass `same_site_origin`.
    """
    server: ServerSettings = websocket.app.state.server
    devices: DeviceStore = websocket.app.state.devices
    headers = websocket.headers
    token = _bearer(headers.get("authorization")) or websocket.query_params.get("token")
    by_cookie = not token
    if by_cookie:
        token = _cookie(headers.get("cookie"))
    principal = authenticate(client_host(websocket.scope), token, server, devices)
    if (
        principal is not None
        and by_cookie
        and principal.device_id is not None
        and not same_site_origin(
            headers.get("origin"), headers.get("referer"), headers.get("host"), server
        )
    ):
        principal = None
    if principal is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return None
    websocket.scope.setdefault("state", {})["principal"] = principal
    return principal
