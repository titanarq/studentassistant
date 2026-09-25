"""One-time pairing codes and the two pairing endpoints (ADR-0001).

`studentassistant pair` asks the running backend for a code (`POST /api/pair/codes`, loopback
only) and shows it as a QR of `{url, code}`; the capture client redeems it once with
`POST /api/pair` and gets its bearer token. Codes live only in this process's memory, expire
`CODE_TTL_SECONDS` after they are minted and can be redeemed once.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from studentassistant.config import ServerSettings
from studentassistant.protocol.rest import PairRequest, PairResponse
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.devices import DeviceStore
from studentassistant.server.network import client_host, is_loopback

log = logging.getLogger(__name__)

CODE_TTL_SECONDS = 5 * 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""No 0/O or 1/I: the code may also be typed by hand from the terminal."""
CODE_GROUP = 4

INVALID_CODE_DETAIL = "invalid pairing code"
"""The one answer for an unknown, expired or already redeemed code: it never says which."""


def _normalise(code: str) -> str:
    return code.replace("-", "").replace(" ", "").upper()


class PairingCodes:
    """The outstanding one-time codes of this process."""

    def __init__(self, clock: Callable[[], float] = time.time, ttl: float = CODE_TTL_SECONDS):
        self._clock = clock
        self._ttl = ttl
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()

    def mint(self) -> tuple[str, float]:
        """A new code (`XXXX-XXXX`) and its expiry as epoch seconds."""
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(2 * CODE_GROUP))
        expires_at = self._clock() + self._ttl
        with self._lock:
            self._purge()
            self._codes[raw] = expires_at
        return f"{raw[:CODE_GROUP]}-{raw[CODE_GROUP:]}", expires_at

    def redeem(self, code: str) -> bool:
        """True exactly once for a code that was minted and has not expired."""
        wanted = _normalise(code)
        with self._lock:
            self._purge()
            match: str | None = None
            for stored in self._codes:
                if hmac.compare_digest(stored.encode(), wanted.encode()):
                    match = stored
            if match is None:
                return False
            del self._codes[match]
            return True

    def _purge(self) -> None:
        now = self._clock()
        for stored, expires_at in list(self._codes.items()):
            if expires_at <= now:
                del self._codes[stored]


class PairingCodeResponse(BaseModel):
    """The answer to `POST /api/pair/codes`: what the pairing QR carries, plus the expiry."""

    url: str
    code: str
    expires_at: datetime


def lan_address() -> str:
    """This PC's address on the LAN, as the default route would use it; loopback if none.

    A UDP `connect` only picks the outgoing interface: no packet is sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def base_url(server: ServerSettings, address: Callable[[], str] = lan_address) -> str:
    """The LAN base URL a capture client should use: `server.public_url` or the LAN address."""
    if server.public_url:
        return server.public_url.rstrip("/")
    return f"http://{address()}:{server.port}"


def pairing_router(server: ServerSettings, devices: DeviceStore, codes: PairingCodes) -> APIRouter:
    router = APIRouter()

    @router.post("/api/pair/codes")
    def mint_code(request: Request) -> PairingCodeResponse:
        if not is_loopback(client_host(request.scope)):
            raise HTTPException(status_code=403, detail="pairing codes are minted on this PC only")
        code, expires_at = codes.mint()
        log.info("pairing code minted, valid for %d s", CODE_TTL_SECONDS)
        return PairingCodeResponse(
            url=base_url(server),
            code=code,
            expires_at=datetime.fromtimestamp(expires_at, UTC),
        )

    @router.post("/api/pair")
    def pair(body: PairRequest) -> PairResponse:
        if not codes.redeem(body.pairing_code):
            log.warning("pairing refused: invalid pairing code")
            raise HTTPException(status_code=401, detail=INVALID_CODE_DETAIL)
        device, token = devices.issue_token(body.device_name, body.protocol_version)
        log.info("device %s paired (%s client)", device.id, body.client_kind)
        return PairResponse(device_id=device.id, token=token, protocol_version=PROTOCOL_VERSION)

    return router
