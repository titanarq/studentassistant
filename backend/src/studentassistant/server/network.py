"""Who may reach the backend at all: loopback and the private LAN only (ADR-0001).

`LanGuardMiddleware` refuses every HTTP request and WebSocket whose client address is neither
loopback nor private (RFC 1918, link-local, IPv6 unique-local). The address is the socket peer
uvicorn reports; no `X-Forwarded-For` is ever trusted, since there is no proxy in front
(`studentassistant serve` runs uvicorn with `proxy_headers=False`).

`HostAllowlistMiddleware` defends against DNS rebinding: a page on `evil.example` whose name is
re-pointed at `127.0.0.1` reaches the backend from loopback, but its requests still carry
`Host: evil.example`. Only names this backend is known by, and IP literals that are loopback or
private (which rebinding cannot produce), pass.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from studentassistant.config import ServerSettings

_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # RFC 1918
        "172.16.0.0/12",  # RFC 1918
        "192.168.0.0/16",  # RFC 1918
        "169.254.0.0/16",  # link-local
        "::1/128",  # loopback
        "fe80::/10",  # link-local
        "fc00::/7",  # unique-local, IPv6's private range
    )
)


def client_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def _address(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_loopback(host: str | None) -> bool:
    address = _address(host)
    return address is not None and address.is_loopback


def is_lan(host: str | None) -> bool:
    """True for loopback and private-LAN addresses; False for anything else or no address."""
    address = _address(host)
    return address is not None and any(address in network for network in _ALLOWED_NETWORKS)


class LanGuardMiddleware:
    """Refuse clients outside loopback and the private ranges: 403 for HTTP, 1008 for WS."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or is_lan(client_host(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": ""})
            return
        await send_json(send, 403, b'{"detail":"only the local network may reach this backend"}')


WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
"""Bind addresses that mean "every interface" rather than one address or name."""


def host_name(host_header: str | None) -> str | None:
    """The host part of a `Host` header (no port, no IPv6 brackets, lower case), or None."""
    if not host_header:
        return None
    value = host_header.strip().lower()
    if value.startswith("["):
        name, bracket, _ = value[1:].partition("]")
        return (name or None) if bracket else None
    if value.count(":") > 1:  # a bare IPv6 literal without brackets
        return value
    return value.partition(":")[0] or None


def allowed_host_names(server: ServerSettings) -> frozenset[str]:
    """The names, beyond loopback/private IP literals, a `Host` header may carry."""
    names: set[str] = {"localhost"}
    if server.host not in WILDCARD_HOSTS:
        names.add(server.host)
    if server.public_url:
        public = urlsplit(server.public_url).hostname
        if public:
            names.add(public)
    names.update(server.allowed_hosts)
    return frozenset(filter(None, (host_name(name) for name in names)))


def is_allowed_host(host_header: str | None, names: Iterable[str]) -> bool:
    """True when `host_header` names this backend: a known name or a loopback/private literal."""
    name = host_name(host_header)
    if name is None:
        return False
    return name in names or is_lan(name)


class HostAllowlistMiddleware:
    """Refuse requests whose `Host` this backend is not known by: 421 for HTTP, 1008 for WS."""

    def __init__(self, app: ASGIApp, server: ServerSettings) -> None:
        self.app = app
        self.names = allowed_host_names(server)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or is_allowed_host(
            _host_header(scope), self.names
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": ""})
            return
        await send_json(send, 421, b'{"detail":"this backend is not known by that host name"}')


def _host_header(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == b"host":
            return value.decode("latin-1")
    return None


async def send_json(send: Send, status: int, body: bytes, headers: list[Any] | None = None) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
