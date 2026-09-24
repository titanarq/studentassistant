"""`GET /api/health` through FastAPI's TestClient: 200, `status == "ok"`, the running version."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studentassistant import __version__
from studentassistant.config import ServerSettings
from studentassistant.server.app import create_app

LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def server(tmp_path: Path) -> ServerSettings:
    """The `[server]` section with the device store under `tmp_path`, never the real config."""
    return ServerSettings(devices_path=tmp_path / "devices.json")


@pytest.fixture
def client(server: ServerSettings) -> TestClient:
    """A client over a freshly built app: it calls the routes without a server or a bound port."""
    return TestClient(create_app(server=server), base_url="http://localhost:8765", client=LOOPBACK)


def test_health_answers_ok_with_the_package_version(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_health_answers_only_the_two_documented_fields(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok", "version": __version__}


def test_create_app_builds_an_app_of_its_own_each_time(server: ServerSettings) -> None:
    assert create_app(server=server) is not create_app(server=server)
