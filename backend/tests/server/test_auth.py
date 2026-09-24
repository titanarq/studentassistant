"""Bearer enforcement for REST and the WebSocket authentication helper."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from studentassistant.config import ServerSettings
from studentassistant.server.app import create_app
from studentassistant.server.auth import EXEMPT_ROUTES, authenticate_websocket

PairDevice = Callable[[], dict[str, Any]]


def _mount_test_routes(app: FastAPI) -> FastAPI:
    """A protected REST route and a WebSocket, mounted only by these tests (the real one is #35)."""

    @app.get("/api/private")
    def private(request: Request) -> dict[str, Any]:
        principal = request.state.principal
        return {"device_id": principal.device_id, "local": principal.local}

    @app.websocket("/ws/test")
    async def socket(websocket: WebSocket) -> None:
        principal = await authenticate_websocket(websocket)
        if principal is None:
            return
        await websocket.accept()
        await websocket.send_json({"device_id": principal.device_id, "local": principal.local})
        await websocket.close()

    return app


@pytest.fixture(autouse=True)
def test_routes(app: FastAPI) -> FastAPI:
    return _mount_test_routes(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# REST


def test_the_exemptions_are_exactly_the_three_public_endpoints() -> None:
    assert {
        ("GET", "/api/health"),
        ("POST", "/api/pair"),
        ("POST", "/api/pair/codes"),
    } == EXEMPT_ROUTES


def test_an_exempt_route_needs_no_token(lan: TestClient) -> None:
    assert lan.get("/api/health").status_code == 200


def test_any_other_route_without_a_token_is_401(lan: TestClient) -> None:
    response = lan.get("/api/private")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert lan.get("/").status_code == 401  # the web app too, from another machine
    assert lan.get("/api/health", headers={}).status_code == 200


def test_a_wrong_token_is_401(lan: TestClient, pair_device: PairDevice) -> None:
    pair_device()

    assert lan.get("/api/private", headers=bearer("sa_wrong")).status_code == 401
    assert lan.get("/api/private", headers={"Authorization": "Basic abc"}).status_code == 401


def test_a_paired_token_passes(lan: TestClient, pair_device: PairDevice) -> None:
    paired = pair_device()

    response = lan.get("/api/private", headers=bearer(paired["token"]))

    assert response.status_code == 200
    assert response.json() == {"device_id": paired["device_id"], "local": False}


def test_a_revoked_token_is_401(lan: TestClient, app: FastAPI, pair_device: PairDevice) -> None:
    paired = pair_device()
    app.state.devices.revoke(paired["device_id"])

    assert lan.get("/api/private", headers=bearer(paired["token"])).status_code == 401


def test_loopback_passes_without_a_token_by_default(local: TestClient) -> None:
    response = local.get("/api/private")

    assert response.status_code == 200
    assert response.json() == {"device_id": None, "local": True}


def test_loopback_is_401_when_localhost_is_not_trusted(devices_path: Path, tmp_path: Path) -> None:
    server = ServerSettings(devices_path=devices_path, trust_localhost=False)
    app = _mount_test_routes(create_app(static_dir=tmp_path / "none", server=server))
    local = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))

    assert local.get("/api/private").status_code == 401
    assert local.get("/api/health").status_code == 200
    # Pairing still works on the PC itself, and its token is then required.
    code = local.post("/api/pair/codes").json()["code"]
    token = local.post(
        "/api/pair",
        json={
            "pairing_code": code,
            "device_name": "PC",
            "client_kind": "web",
            "protocol_version": "1.0",
        },
    ).json()["token"]
    assert local.get("/api/private", headers=bearer(token)).status_code == 200


def test_trust_localhost_defaults_to_true() -> None:
    assert ServerSettings().trust_localhost is True


# WebSocket


def test_a_websocket_with_a_header_token_is_accepted(
    lan: TestClient, pair_device: PairDevice
) -> None:
    paired = pair_device()

    with lan.websocket_connect("/ws/test", headers=bearer(paired["token"])) as socket:
        assert socket.receive_json() == {"device_id": paired["device_id"], "local": False}


def test_a_websocket_with_a_query_token_is_accepted(
    lan: TestClient, pair_device: PairDevice
) -> None:
    paired = pair_device()

    with lan.websocket_connect(f"/ws/test?token={paired['token']}") as socket:
        assert socket.receive_json()["device_id"] == paired["device_id"]


def test_a_websocket_without_a_token_is_closed_with_1008(lan: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as closed, lan.websocket_connect("/ws/test"):
        pass

    assert closed.value.code == 1008


def test_a_websocket_with_a_revoked_token_is_closed_with_1008(
    lan: TestClient, app: FastAPI, pair_device: PairDevice
) -> None:
    paired = pair_device()
    app.state.devices.revoke(paired["device_id"])

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        lan.websocket_connect(f"/ws/test?token={paired['token']}"),
    ):
        pass

    assert closed.value.code == 1008


def test_a_loopback_websocket_is_trusted_like_rest(local: TestClient) -> None:
    with local.websocket_connect("/ws/test") as socket:
        assert socket.receive_json() == {"device_id": None, "local": True}
