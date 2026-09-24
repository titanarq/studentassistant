"""Only loopback and the private LAN reach the backend; `serve` binds the configured address."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.server.network import is_lan

ClientAt = Callable[[str], TestClient]


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "100.64.0.1", "172.32.0.1", "192.0.2.10", "2001:4860::8888", "testclient"]
)
def test_a_public_or_unknown_client_is_refused(client_at: ClientAt, host: str) -> None:
    response = client_at(host).get("/api/health")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.9",
        "172.31.255.254",
        "192.168.1.30",
        "169.254.10.20",
        "fe80::1",
        "fd12:3456::1",
        "::ffff:192.168.1.30",
    ],
)
def test_loopback_and_private_clients_are_accepted(client_at: ClientAt, host: str) -> None:
    assert client_at(host).get("/api/health").status_code == 200


def test_a_public_websocket_is_closed_with_1008(client_at: ClientAt) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client_at("8.8.8.8").websocket_connect("/ws"),
    ):
        pass

    assert closed.value.code == 1008


def test_no_address_is_not_lan() -> None:
    assert is_lan(None) is False
    assert is_lan("") is False


def test_serve_binds_the_configured_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("SA_SERVER__HOST", "192.168.1.20")
    monkeypatch.setenv("SA_SERVER__PORT", "9200")

    result = CliRunner().invoke(cli, ["serve"])

    assert result.exit_code == 0
    assert calls == [{"host": "192.168.1.20", "port": 9200}]
