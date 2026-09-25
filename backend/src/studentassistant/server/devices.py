"""Paired capture clients and their bearer tokens (ADR-0001).

A device is paired once: `issue_token` generates a random token, hands the plaintext back to the
caller exactly once and keeps only a salted SHA-256 hash of it in `devices.json`. The file lives
at `server.devices_path` (default `~/.local/share/studentassistant/devices.json`), never inside the
vault, and is always written with mode 600. Every read goes back to the file, so a device revoked
by `studentassistant devices revoke` in another process stops being accepted at once.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

TOKEN_PREFIX = "sa_"
TOKEN_BYTES = 32
"""Bytes of `secrets` entropy in every token (`secrets.token_urlsafe` -> 43 characters)."""

PAIRED_BEFORE_VERSIONING = "1.0"
"""The protocol version of a device whose pairing record carries none (paired before 1.1)."""

FILE_MODE = 0o600
DIR_MODE = 0o700


class Device(BaseModel):
    """One paired capture client as stored on disk: never the plaintext token."""

    id: str
    name: str
    created_at: datetime
    token_salt: str
    token_hash: str
    # The `protocol_version` the client sent when it paired; absent for a device paired before
    # protocol 1.1, which is therefore a 1.0 client.
    protocol_version: str | None = None


class DeviceInfo(BaseModel):
    """What may be shown about a device: no salt, no hash, no token."""

    id: str
    name: str
    created_at: datetime
    protocol_version: str = PAIRED_BEFORE_VERSIONING


class _DevicesFile(BaseModel):
    devices: list[Device] = Field(default_factory=list)


def _hash_token(salt: str, token: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + token.encode("utf-8")).hexdigest()


class DeviceStore:
    """The paired devices kept in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def issue_token(
        self, name: str, protocol_version: str = PAIRED_BEFORE_VERSIONING
    ) -> tuple[DeviceInfo, str]:
        """Pair a new device called `name` speaking `protocol_version` (the one its pairing
        request carried); return it and its plaintext token (shown only now)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        salt = secrets.token_hex(16)
        device = Device(
            id="dev-" + secrets.token_hex(6),
            name=name,
            created_at=datetime.now(UTC),
            token_salt=salt,
            token_hash=_hash_token(salt, token),
            protocol_version=protocol_version,
        )
        with self._lock:
            stored = self._read()
            stored.devices.append(device)
            self._write(stored)
        return _info(device), token

    def verify_token(self, token: str) -> DeviceInfo | None:
        """The device `token` belongs to, or None when it is unknown or revoked."""
        if not token:
            return None
        found: Device | None = None
        # Every stored hash is compared, found or not, so the time taken says nothing.
        for device in self._read().devices:
            candidate = _hash_token(device.token_salt, token)
            if hmac.compare_digest(candidate, device.token_hash) and found is None:
                found = device
        return _info(found) if found is not None else None

    def list_devices(self) -> list[DeviceInfo]:
        return [_info(device) for device in self._read().devices]

    def revoke(self, device_id: str) -> bool:
        """Remove the device `device_id`; False when there is no such device."""
        with self._lock:
            stored = self._read()
            kept = [device for device in stored.devices if device.id != device_id]
            if len(kept) == len(stored.devices):
                return False
            self._write(_DevicesFile(devices=kept))
        return True

    def _read(self) -> _DevicesFile:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _DevicesFile()
        return _DevicesFile.model_validate_json(raw)

    def _write(self, stored: _DevicesFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        # mkstemp creates the file with mode 600; the rename makes the update atomic.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".devices-", suffix=".json")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(stored.model_dump_json(indent=2).encode("utf-8"))
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        os.chmod(self.path, FILE_MODE)


def _info(device: Device) -> DeviceInfo:
    return DeviceInfo(
        id=device.id,
        name=device.name,
        created_at=device.created_at,
        protocol_version=device.protocol_version or PAIRED_BEFORE_VERSIONING,
    )
