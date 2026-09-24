"""Who may reach the backend at all: loopback and the private LAN only (ADR-0001).

`LanGuardMiddleware` refuses every HTTP request and WebSocket whose client address is neither
loopback nor private (RFC 1918, link-local, IPv6 unique-local). The address is the socket peer
uvicorn reports; no `X-Forwarded-For` is ever trusted, since there is no proxy in front.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

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
