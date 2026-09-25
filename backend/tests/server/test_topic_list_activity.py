"""`GET /api/subjects/{subject_id}/topics` fills `last_session_at_ms` and `pending_count` (1.1).

Peers speak the lower MINOR and a client refuses unknown fields, so a device that paired as a 1.0
client never gets them; the PC itself (loopback, the `reader`) speaks this backend's version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from read_api_fixtures import ReadVault

from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.protocol import TopicsListResponse
from studentassistant.vault import list_sessions, resume_session

SCHEMA = Path(__file__).resolve().parents[3] / "protocol" / "rest.topics.list.response.schema.json"


def listed(
    reader: TestClient, subject: str, headers: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """The topic list, checked against the schema and the model, keyed by `topic_id`."""
    response = reader.get(f"/api/subjects/{subject}/topics", headers=headers)
    assert response.status_code == 200
    body = response.json()
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(body)
    TopicsListResponse.model_validate(body)
    return {topic["topic_id"]: topic for topic in body["topics"]}


def test_a_studied_topic_carries_its_latest_session_start_and_open_pending_count(
    read_vault: ReadVault, reader: TestClient
) -> None:
    topic = listed(reader, read_vault.subject)[read_vault.topic]

    latest = list_sessions(read_vault.vault, read_vault.subject, read_vault.topic)[-1]
    assert latest.id == read_vault.open_session
    assert topic["last_session_at_ms"] == int(latest.started_at.timestamp() * 1000)
    # Two pending items were added and one resolved.
    assert topic["pending_count"] == 1
    assert topic["open_session_id"] == read_vault.open_session


def test_a_topic_without_sessions_leaves_the_date_out_and_counts_zero(
    read_vault: ReadVault, reader: TestClient
) -> None:
    topic = listed(reader, read_vault.subject)[read_vault.empty_topic]

    assert "last_session_at_ms" not in topic
    assert "open_session_id" not in topic
    assert topic["pending_count"] == 0


def test_an_unfoldable_observer_log_leaves_only_the_pending_count_out(
    read_vault: ReadVault, reader: TestClient
) -> None:
    session = resume_session(read_vault.vault, read_vault.subject, read_vault.topic)
    session.append_event(
        STATE_OP_EVENT_KIND,
        "observer",
        {"op": "resolve_pending", "pending_id": "missing", "resolution": "x"},
    )

    topic = listed(reader, read_vault.subject)[read_vault.topic]

    assert "pending_count" not in topic
    assert "last_session_at_ms" in topic
    assert listed(reader, read_vault.subject)[read_vault.empty_topic]["pending_count"] == 0


def paired_as(reader: TestClient, lan_reader: TestClient, version: str) -> dict[str, str]:
    """Pair a LAN device that sends `version` in its pairing request: its bearer header."""
    code = reader.post("/api/pair/codes").json()["code"]
    body = {
        "pairing_code": code,
        "device_name": "Móvil de Lucía",
        "client_kind": "android",
        "protocol_version": version,
    }
    response = lan_reader.post("/api/pair", json=body)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_a_1_0_client_gets_topics_without_the_1_1_fields(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient
) -> None:
    headers = paired_as(reader, lan_reader, "1.0")

    topics = listed(lan_reader, read_vault.subject, headers)

    for topic in topics.values():
        assert "last_session_at_ms" not in topic and "pending_count" not in topic
    # Exactly the 1.0 shape: what a strict 1.0 decoder accepts.
    assert set(topics[read_vault.topic]) == {"topic_id", "subject_id", "name", "open_session_id"}


def test_a_1_1_client_gets_both_fields(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient
) -> None:
    headers = paired_as(reader, lan_reader, "1.1")

    topics = listed(lan_reader, read_vault.subject, headers)

    assert topics[read_vault.topic]["pending_count"] == 1
    assert "last_session_at_ms" in topics[read_vault.topic]
    assert topics[read_vault.empty_topic]["pending_count"] == 0


def test_a_newer_minor_client_is_answered_in_1_1(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient
) -> None:
    headers = paired_as(reader, lan_reader, "1.7")

    assert "pending_count" in listed(lan_reader, read_vault.subject, headers)[read_vault.topic]
