"""`GET /api/health` through FastAPI's TestClient: 200, `status == "ok"`, the running version."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studentassistant import __version__
from studentassistant.server.app import create_app


@pytest.fixture
def client() -> TestClient:
    """A client over a freshly built app: it calls the routes without a server or a bound port."""
    return TestClient(create_app())


def test_health_answers_ok_with_the_package_version(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_health_answers_only_the_two_documented_fields(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok", "version": __version__}


def test_create_app_builds_an_app_of_its_own_each_time() -> None:
    assert create_app() is not create_app()
