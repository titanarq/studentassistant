"""The device store: tokens issued once, verified by salted hash, revocable, never on disk."""

from __future__ import annotations

import stat
from pathlib import Path

from studentassistant.server.devices import TOKEN_BYTES, DeviceStore


def test_an_issued_token_verifies_as_its_device(devices_path: Path) -> None:
    store = DeviceStore(devices_path)

    device, token = store.issue_token("Móvil de Lucía")

    verified = store.verify_token(token)
    assert verified is not None
    assert verified.id == device.id
    assert verified.name == "Móvil de Lucía"


def test_a_token_carries_at_least_32_bytes_of_entropy(devices_path: Path) -> None:
    _, token = DeviceStore(devices_path).issue_token("a")

    assert TOKEN_BYTES >= 32
    assert len(token) >= 43  # token_urlsafe(32): 43 base64url characters


def test_two_devices_get_different_tokens(devices_path: Path) -> None:
    store = DeviceStore(devices_path)

    first, first_token = store.issue_token("uno")
    second, second_token = store.issue_token("dos")

    assert first_token != second_token
    assert first.id != second.id
    assert store.verify_token(second_token).id == second.id  # type: ignore[union-attr]


def test_an_unknown_token_is_rejected(devices_path: Path) -> None:
    store = DeviceStore(devices_path)
    store.issue_token("uno")

    assert store.verify_token("sa_not-a-token-of-ours") is None
    assert store.verify_token("") is None


def test_an_empty_store_rejects_everything(devices_path: Path) -> None:
    assert DeviceStore(devices_path).verify_token("anything") is None
    assert DeviceStore(devices_path).list_devices() == []


def test_a_revoked_token_is_rejected(devices_path: Path) -> None:
    store = DeviceStore(devices_path)
    device, token = store.issue_token("uno")
    _, kept_token = store.issue_token("dos")

    assert store.revoke(device.id) is True

    assert store.verify_token(token) is None
    assert store.verify_token(kept_token) is not None
    assert [listed.name for listed in store.list_devices()] == ["dos"]


def test_revoking_an_unknown_device_says_so(devices_path: Path) -> None:
    assert DeviceStore(devices_path).revoke("dev-nope") is False


def test_a_revocation_by_another_store_on_the_same_file_is_seen(devices_path: Path) -> None:
    server_side = DeviceStore(devices_path)
    device, token = server_side.issue_token("uno")

    DeviceStore(devices_path).revoke(device.id)  # the CLI, in another process

    assert server_side.verify_token(token) is None


def test_the_plaintext_token_is_never_written(devices_path: Path) -> None:
    store = DeviceStore(devices_path)
    _, token = store.issue_token("uno")

    raw = devices_path.read_bytes()
    assert token.encode() not in raw
    assert token.removeprefix("sa_").encode() not in raw


def test_the_file_is_private_to_its_owner(devices_path: Path) -> None:
    store = DeviceStore(devices_path)
    device, _ = store.issue_token("uno")
    store.issue_token("dos")
    store.revoke(device.id)

    assert stat.S_IMODE(devices_path.stat().st_mode) == 0o600
    assert list(devices_path.parent.iterdir()) == [devices_path]  # no temporary file left


def test_listing_shows_id_name_and_creation_time_only(devices_path: Path) -> None:
    store = DeviceStore(devices_path)
    device, _ = store.issue_token("uno")

    [listed] = store.list_devices()

    assert set(listed.model_dump()) == {"id", "name", "created_at"}
    assert listed == device
