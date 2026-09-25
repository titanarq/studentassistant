"""`GET /api/subjects/{subject_id}/topics` fills `last_session_at_ms` and `pending_count` (1.1)
and `digest_excerpt` (1.3).

Peers speak the lower MINOR and a client refuses unknown fields, so a device that paired as an
older client never gets the newer fields; the PC itself (loopback, the `reader`) speaks this
backend's version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from read_api_fixtures import ReadVault

from studentassistant.observer import STATE_OP_EVENT_KIND
from studentassistant.protocol import DIGEST_EXCERPT_MAX, TopicsListResponse
from studentassistant.vault import (
    list_sessions,
    resume_session,
    topic_digest_path,
    write_topic_digest,
)

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


DIGEST = (
    "# Resumen del tema: Cinemática\n\n"
    "1 sesión con contenido; la última, el 24/09/2026: MRU. Sin dudas abiertas.\n\n"
    "Asignatura: Física.\n"
)
EXCERPT = "1 sesión con contenido; la última, el 24/09/2026: MRU. Sin dudas abiertas."


def test_a_topic_with_a_digest_carries_its_summary_paragraph(
    read_vault: ReadVault, reader: TestClient
) -> None:
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, DIGEST)

    topics = listed(reader, read_vault.subject)

    assert topics[read_vault.topic]["digest_excerpt"] == EXCERPT
    # No session has ended on it yet, so no digest: the excerpt is left out.
    assert "digest_excerpt" not in topics[read_vault.empty_topic]


def test_a_long_summary_is_cut_to_the_protocol_limit(
    read_vault: ReadVault, reader: TestClient
) -> None:
    summary = "Movimiento rectilíneo uniforme y acelerado. " * 40
    text = f"# R\n\n{summary}\n"
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, text)

    excerpt = listed(reader, read_vault.subject)[read_vault.topic]["digest_excerpt"]

    assert len(excerpt) == DIGEST_EXCERPT_MAX
    assert excerpt.endswith("…")


def test_an_unreadable_digest_leaves_only_the_excerpt_out(
    read_vault: ReadVault, reader: TestClient
) -> None:
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, DIGEST)
    # Damage the file the way a bad sync could: not UTF-8 any more.
    path = topic_digest_path(read_vault.vault, read_vault.subject, read_vault.topic)
    path.write_bytes(b"\xff\xfe")

    topic = listed(reader, read_vault.subject)[read_vault.topic]

    assert "digest_excerpt" not in topic
    assert topic["pending_count"] == 1


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_a_pre_1_3_client_never_gets_the_excerpt(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient, version: str
) -> None:
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, DIGEST)
    headers = paired_as(reader, lan_reader, version)

    topic = listed(lan_reader, read_vault.subject, headers)[read_vault.topic]

    assert "digest_excerpt" not in topic
    assert ("pending_count" in topic) is (version != "1.0")


def test_a_1_3_client_gets_the_excerpt(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient
) -> None:
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, DIGEST)
    headers = paired_as(reader, lan_reader, "1.3")

    topic = listed(lan_reader, read_vault.subject, headers)[read_vault.topic]

    assert topic["digest_excerpt"] == EXCERPT
    assert topic["pending_count"] == 1


def test_a_newer_minor_client_is_answered_in_our_version(
    read_vault: ReadVault, reader: TestClient, lan_reader: TestClient
) -> None:
    write_topic_digest(read_vault.vault, read_vault.subject, read_vault.topic, DIGEST)
    headers = paired_as(reader, lan_reader, "1.7")

    topic = listed(lan_reader, read_vault.subject, headers)[read_vault.topic]

    assert "pending_count" in topic
    assert topic["digest_excerpt"] == EXCERPT
