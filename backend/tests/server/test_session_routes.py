"""The subjects/topics/sessions REST routes of protocol v1, through FastAPI's TestClient.

Every body is checked against both halves of the contract: the JSON Schema under `protocol/` and
the backend's strict protocol model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from studentassistant.config import ServerSettings
from studentassistant.protocol import model_for
from studentassistant.server.app import create_app
from studentassistant.server.auth import EXEMPT_ROUTES
from studentassistant.server.bus import SessionBus
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import SESSION_ENDED, SESSION_STARTED, SessionService
from studentassistant.vault import Event, Vault, read_jsonl

PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"

PairDevice = Callable[[], dict[str, Any]]


@pytest.fixture
def app(server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=tmp_vault
    )


def conforms(name: str, body: Any) -> Any:
    """`body` validates against `protocol/<name>.schema.json` and the registered model."""
    schema = json.loads((PROTOCOL_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    model_for(name).model_validate(body)
    return body


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_new_routes_are_not_exempt_from_the_bearer_check() -> None:
    assert {path for _, path in EXEMPT_ROUTES} == {"/api/health", "/api/pair", "/api/pair/codes"}


def test_the_service_and_the_bus_live_on_app_state(app: FastAPI) -> None:
    assert isinstance(app.state.bus, SessionBus)
    assert isinstance(app.state.sessions, SessionService)
    assert app.state.sessions.bus is app.state.bus


def test_a_paired_device_runs_a_whole_session(
    app: FastAPI, lan: TestClient, pair_device: PairDevice, tmp_vault: Vault
) -> None:
    headers = bearer(pair_device()["token"])

    assert conforms("rest.subjects.list.response", lan.get("/api/subjects", headers=headers).json())
    created = lan.post("/api/subjects", json={"name": "Física"}, headers=headers)
    assert created.status_code == 201
    subject = conforms("rest.subjects.create.response", created.json())
    assert subject == {"subject_id": "fisica", "name": "Física"}

    created = lan.post("/api/subjects/fisica/topics", json={"name": "Cinemática"}, headers=headers)
    assert created.status_code == 201
    topic = conforms("rest.topics.create.response", created.json())
    assert topic == {"topic_id": "cinematica", "subject_id": "fisica", "name": "Cinemática"}

    started = lan.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1_000},
        headers=headers,
    )
    assert started.status_code == 201
    session = conforms("rest.sessions.start.response", started.json())
    session_id = session["session_id"]
    assert session["ws_path"] == f"/ws/sessions/{session_id}"

    listed = conforms(
        "rest.topics.list.response", lan.get("/api/subjects/fisica/topics", headers=headers).json()
    )
    assert listed["topics"][0]["open_session_id"] == session_id

    resumed = lan.post(f"/api/sessions/{session_id}/resume", headers=headers)
    assert resumed.status_code == 200
    assert conforms("rest.sessions.resume.response", resumed.json()) == session

    ended = lan.post(
        f"/api/sessions/{session_id}/end",
        json={"client_time_ms": 2_000, "reason": "button"},
        headers=headers,
    )
    assert ended.status_code == 200
    body = conforms("rest.sessions.end.response", ended.json())
    assert (body["session_id"], body["status"]) == (session_id, "ended")

    log = (
        tmp_vault.path / "subjects/fisica/topics/cinematica/sessions" / session_id / "events.jsonl"
    )
    events = list(read_jsonl(log, Event))
    assert [e.kind for e in events] == [SESSION_STARTED, "session.resumed", SESSION_ENDED]
    assert events[0].payload["device_id"] is not None


def test_a_second_active_session_is_a_409(local: TestClient) -> None:
    local.post("/api/subjects", json={"name": "Física"})
    local.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
    local.post("/api/subjects/fisica/topics", json={"name": "Dinámica"})
    first = local.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1},
    ).json()

    second = local.post(
        "/api/sessions", json={"subject_id": "fisica", "topic_id": "dinamica", "client_time_ms": 2}
    )
    assert second.status_code == 409
    assert first["session_id"] in second.json()["detail"]
    assert second.json()["code"] == "session_open"
    assert second.headers["X-Open-Session-Id"] == first["session_id"]


@pytest.mark.parametrize(("paired_as", "coded"), [("1.0", False), ("1.1", False), ("1.2", True)])
def test_the_error_code_follows_the_devices_protocol_version(
    local: TestClient, lan: TestClient, paired_as: str, coded: bool
) -> None:
    code = local.post("/api/pair/codes").json()["code"]
    pairing = {
        "pairing_code": code,
        "device_name": "Móvil",
        "client_kind": "android",
        "protocol_version": paired_as,
    }
    headers = bearer(lan.post("/api/pair", json=pairing).json()["token"])
    local.post("/api/subjects", json={"name": "Física"})
    local.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
    local.post("/api/subjects/fisica/topics", json={"name": "Dinámica"})
    start = {"subject_id": "fisica", "client_time_ms": 1}
    assert local.post("/api/sessions", json={**start, "topic_id": "cinematica"}).status_code == 201

    refused = lan.post("/api/sessions", json={**start, "topic_id": "dinamica"}, headers=headers)

    assert refused.status_code == 409 and "X-Open-Session-Id" in refused.headers
    # A 1.0/1.1 client gets the body it knows; `code` arrived in 1.2.
    expected_keys = {"detail", "code"} if coded else {"detail"}
    assert set(refused.json()) == expected_keys


def test_unknown_things_are_404_and_ended_sessions_409(local: TestClient) -> None:
    assert local.get("/api/subjects/nada/topics").status_code == 404
    assert local.post("/api/subjects/nada/topics", json={"name": "Tema"}).status_code == 404
    local.post("/api/subjects", json={"name": "Física"})
    missing_topic = local.post(
        "/api/sessions", json={"subject_id": "fisica", "topic_id": "nada", "client_time_ms": 1}
    )
    assert missing_topic.status_code == 404
    assert local.post("/api/sessions/20000101-000000/resume").status_code == 404

    local.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
    session_id = local.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1},
    ).json()["session_id"]
    end = {"client_time_ms": 2, "reason": "button"}
    assert local.post(f"/api/sessions/{session_id}/end", json=end).status_code == 200
    assert local.post(f"/api/sessions/{session_id}/end", json=end).status_code == 409
    assert local.post(f"/api/sessions/{session_id}/resume").status_code == 409


def test_bodies_are_validated_with_the_protocol_models(local: TestClient) -> None:
    assert local.post("/api/subjects", json={"name": ""}).status_code == 422
    assert local.post("/api/subjects", json={"name": "Física", "extra": 1}).status_code == 422
    local.post("/api/subjects", json={"name": "Física"})
    local.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
    no_client_time = local.post(
        "/api/sessions", json={"subject_id": "fisica", "topic_id": "cinematica"}
    )
    assert no_client_time.status_code == 422
    # A path id outside the protocol's id pattern never reaches the vault.
    assert local.get("/api/subjects/.hidden/topics").status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/subjects"),
        ("POST", "/api/subjects"),
        ("GET", "/api/subjects/fisica/topics"),
        ("POST", "/api/subjects/fisica/topics"),
        ("POST", "/api/sessions"),
        ("POST", "/api/sessions/20260924-180000/resume"),
        ("POST", "/api/sessions/20260924-180000/end"),
    ],
)
def test_a_lan_client_without_a_token_gets_401(lan: TestClient, method: str, path: str) -> None:
    response = lan.request(method, path, json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_wrong_host_never_reaches_the_routes(
    app: FastAPI, client_with_host: Callable[[FastAPI, str], TestClient]
) -> None:
    assert client_with_host(app, "evil.example").get("/api/subjects").status_code == 421


def test_a_public_address_never_reaches_the_routes(public: TestClient) -> None:
    assert public.get("/api/subjects").status_code == 403
