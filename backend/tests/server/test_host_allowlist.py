"""The Host allowlist: a DNS-rebinding page reaches loopback, but its `Host` gives it away."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from studentassistant.config import ServerSettings
from studentassistant.server.app import create_app
from studentassistant.server.network import allowed_host_names, host_name, is_allowed_host

ClientWithHost = Callable[[FastAPI, str], TestClient]


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "127.0.0.1.evil.example"])
def test_a_rebinding_host_from_loopback_is_421(
    client_with_host: ClientWithHost, app: FastAPI, host: str
) -> None:
    client = client_with_host(app, host)

    health = client.get("/api/health")
    mint = client.post("/api/pair/codes")

    assert health.status_code == 421
    assert mint.status_code == 421
    assert "code" not in mint.json()


def test_a_missing_host_is_refused(client_with_host: ClientWithHost, app: FastAPI) -> None:
    client = client_with_host(app, "localhost")

    response = client.get("/api/health", headers={"host": ""})

    assert response.status_code == 421


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST:8765",
        "127.0.0.1",
        "127.0.0.1:8765",
        "[::1]:8765",
        "192.168.1.20:8765",  # the fixture's `public_url`
        "10.0.0.5:8765",  # any other private literal: the PC's LAN address, whatever it is
        "[fe80::1]:8765",
    ],
)
def test_names_this_backend_is_known_by_pass(
    client_with_host: ClientWithHost, app: FastAPI, host: str
) -> None:
    assert client_with_host(app, host).get("/api/health").status_code == 200


def test_public_url_and_allowed_hosts_names_pass(
    client_with_host: ClientWithHost, tmp_path: Path
) -> None:
    server = ServerSettings(
        devices_path=tmp_path / "devices.json",
        public_url="http://studypc.lan:8765",
        allowed_hosts=["MyPC.local"],
    )
    app = create_app(static_dir=tmp_path / "no-web-build", server=server)

    assert client_with_host(app, "studypc.lan:8765").get("/api/health").status_code == 200
    assert client_with_host(app, "mypc.local:8765").get("/api/health").status_code == 200
    assert client_with_host(app, "other.lan:8765").get("/api/health").status_code == 421


def test_a_named_bind_host_is_allowed_and_a_wildcard_one_adds_nothing() -> None:
    assert "studypc.lan" in allowed_host_names(ServerSettings(host="studypc.lan"))
    assert allowed_host_names(ServerSettings(host="0.0.0.0")) == {"localhost"}


def test_a_public_ip_literal_is_not_this_backend() -> None:
    assert is_allowed_host("8.8.8.8:8765", {"localhost"}) is False


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("LocalHost:8765", "localhost"),
        ("[::1]:8765", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("192.168.1.20", "192.168.1.20"),
        ("", None),
        (None, None),
        ("[::1", None),
    ],
)
def test_host_name_strips_the_port_and_brackets(header: str | None, name: str | None) -> None:
    assert host_name(header) == name


def test_a_websocket_with_a_rebinding_host_is_closed_with_1008(
    client_with_host: ClientWithHost, app: FastAPI
) -> None:
    @app.websocket("/ws/host-test")
    async def accept(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"ok": True})

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client_with_host(app, "evil.example:8765").websocket_connect("/ws/host-test"),
    ):
        pass

    assert closed.value.code == 1008
    with client_with_host(app, "localhost:8765").websocket_connect("/ws/host-test") as socket:
        assert socket.receive_json() == {"ok": True}
