"""`GET /api/health` through FastAPI's TestClient: 200 and exactly the protocol v1 health response.

The body is checked against both halves of the contract: the JSON Schema in
`protocol/rest.health.response.schema.json` and the backend's strict `rest.health.response` model,
and it carries the same keys as the shared example (strict clients reject any other key).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from studentassistant.config import ServerSettings
from studentassistant.protocol import PROTOCOL_VERSION, model_for
from studentassistant.server.app import create_app

LOOPBACK = ("127.0.0.1", 50000)
PROTOCOL_DIR = Path(__file__).resolve().parents[2] / "protocol"
HEALTH = "rest.health.response"


def _load(name: str) -> dict[str, object]:
    return json.loads((PROTOCOL_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def server(tmp_path: Path) -> ServerSettings:
    """The `[server]` section with the device store under `tmp_path`, never the real config."""
    return ServerSettings(devices_path=tmp_path / "devices.json")


@pytest.fixture
def client(server: ServerSettings) -> TestClient:
    """A client over a freshly built app: it calls the routes without a server or a bound port."""
    return TestClient(create_app(server=server), base_url="http://localhost:8765", client=LOOPBACK)


def test_health_answers_ok_with_the_protocol_version_and_server_time(client: TestClient) -> None:
    before = time.time_ns() // 1_000_000
    response = client.get("/api/health")
    after = time.time_ns() // 1_000_000

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert isinstance(body["server_time_ms"], int)
    assert before <= body["server_time_ms"] <= after


def test_health_validates_against_the_protocol_schema_and_model(client: TestClient) -> None:
    body = client.get("/api/health").json()

    schema = _load(f"{HEALTH}.schema.json")
    Draft202012Validator(schema).validate(body)
    assert model_for(HEALTH).model_validate(body).model_dump(mode="json") == body


def test_health_has_exactly_the_keys_of_the_protocol_example(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert sorted(body) == sorted(_load(f"examples/{HEALTH}.json"))


def test_create_app_builds_an_app_of_its_own_each_time(server: ServerSettings) -> None:
    assert create_app(server=server) is not create_app(server=server)
