"""Review sessions (a doubt's resolution) are not study sessions on the study desk (#191).

The topic list's `last_session_at_ms` and the topic card's `sessions`/`session_minutes` leave them
out; the topic's session list keeps them, labelled «Revisión de dudas».
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from read_api_fixtures import ReadVault

from studentassistant.vault import end_session, list_sessions, start_session


def _study_then_review(read_vault: ReadVault) -> tuple[str, str]:
    """On the fixture's empty topic: a 20-minute study session, then a review session."""
    vault, subject, topic = read_vault.vault, read_vault.subject, read_vault.empty_topic
    study = start_session(vault, subject, topic, host="ubuntu-pc", protocol_version="1.0")
    end_session(study, ended_at=study.meta.started_at + timedelta(minutes=20))
    review = start_session(
        vault, subject, topic, host="ubuntu-pc", protocol_version="1.0", kind="review"
    )
    end_session(review)
    return study.id, review.id


def test_the_topic_list_dates_the_topic_by_its_latest_study_session(
    read_vault: ReadVault, reader: TestClient
) -> None:
    study_id, _ = _study_then_review(read_vault)
    vault, subject, topic = read_vault.vault, read_vault.subject, read_vault.empty_topic

    study = next(meta for meta in list_sessions(vault, subject, topic) if meta.id == study_id)

    response = reader.get(f"/api/subjects/{subject}/topics")
    assert response.status_code == 200
    listed = {entry["topic_id"]: entry for entry in response.json()["topics"]}
    assert listed[topic]["last_session_at_ms"] == int(study.started_at.timestamp() * 1000)


def test_a_topic_with_only_review_sessions_has_no_last_session(
    read_vault: ReadVault, reader: TestClient
) -> None:
    vault, subject, topic = read_vault.vault, read_vault.subject, read_vault.empty_topic
    end_session(
        start_session(
            vault, subject, topic, host="ubuntu-pc", protocol_version="1.0", kind="review"
        )
    )

    listed = {
        e["topic_id"]: e for e in reader.get(f"/api/subjects/{subject}/topics").json()["topics"]
    }
    assert "last_session_at_ms" not in listed[topic]
    summary = reader.get(f"/api/subjects/{subject}/topics/{topic}/summary").json()
    assert (summary["sessions"], summary["session_minutes"]) == (0, 0.0)


def test_the_topic_card_counts_only_study_sessions(
    read_vault: ReadVault, reader: TestClient
) -> None:
    _study_then_review(read_vault)

    summary = reader.get(
        f"/api/subjects/{read_vault.subject}/topics/{read_vault.empty_topic}/summary"
    ).json()
    assert summary["sessions"] == 1
    assert summary["session_minutes"] == 20.0


def test_the_session_list_labels_the_review_session(
    read_vault: ReadVault, reader: TestClient
) -> None:
    study_id, review_id = _study_then_review(read_vault)

    body = reader.get(
        f"/api/subjects/{read_vault.subject}/topics/{read_vault.empty_topic}/sessions"
    ).json()
    study, review = body["sessions"]
    assert (study["session_id"], study["kind"], study["label"]) == (
        study_id,
        "study",
        "Sesión de estudio",
    )
    assert (review["session_id"], review["kind"], review["label"]) == (
        review_id,
        "review",
        "Revisión de dudas",
    )
