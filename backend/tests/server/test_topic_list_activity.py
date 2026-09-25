"""`GET /api/subjects/{subject_id}/topics` fills `last_session_at_ms` and `pending_count` (1.1)."""

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


def listed(reader: TestClient, subject: str) -> dict[str, dict[str, Any]]:
    """The topic list, checked against the schema and the model, keyed by `topic_id`."""
    response = reader.get(f"/api/subjects/{subject}/topics")
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
