"""The practice waiting today across every topic: `GET /api/practice/summary` (#280)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studentassistant.config import ServerSettings
from studentassistant.generators.flashcards import YAML_NAME, FlashcardsFile, render_yaml
from studentassistant.generators.practice import (
    PracticeReview,
    PracticeSuspension,
    flashcard_item_key,
    practice_summary,
)
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_subject, create_topic, write_generated
from studentassistant.vault.jsonl import append_jsonl
from studentassistant.vault.study import study_log_path

LOCAL_BASE_URL = "http://localhost:8765"
PUBLIC_URL = "http://192.168.1.20:8765"
LAN_HOST = "192.168.1.30"
PAST = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


@pytest.fixture
def app(devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> object:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path, public_url=PUBLIC_URL),
        codes=codes,
        vault=tmp_vault,
    )


@pytest.fixture
def client(app: object) -> Iterator[TestClient]:
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:  # type: ignore[arg-type]
        yield client


def _topic(vault: Vault, subject: str, title: str, cards: int) -> tuple[str, list[str]]:
    """Create `title` under the subject slug `subject` with `cards` flashcards; their item keys."""
    slug = create_topic(vault, subject, title).slug
    ids = [f"c{index:08x}" for index in range(cards)]
    if cards:
        deck = FlashcardsFile(
            deck=title,
            deck_id=1 << 30,
            cards=[{"id": i, "front": f"¿{i}?", "back": i} for i in ids],  # type: ignore[misc]
        )
        write_generated(vault, subject, slug, YAML_NAME, render_yaml(deck))
    return slug, [flashcard_item_key(i) for i in ids]


def _review(vault: Vault, subject: str, topic: str, key: str, rating: str = "again") -> None:
    path = study_log_path(vault, subject, topic, "practice")
    path.parent.mkdir(parents=True, exist_ok=True)
    review = PracticeReview(time=PAST, item=key, source="flashcards", rating=rating)  # type: ignore[arg-type]
    append_jsonl(path, review)


def test_no_topics(client: TestClient) -> None:
    response = client.get("/api/practice/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["topics"] == [] and body["warnings"] == []
    assert body["totals"] == {"due": 0, "new": 0, "topics": 0}


def test_one_topic_due(client: TestClient, tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    create_topic(tmp_vault, subject, "Sin material")  # omitted: neither flashcards nor quiz
    topic, keys = _topic(tmp_vault, subject, "Derivadas", 3)
    _review(tmp_vault, subject, topic, keys[0])  # `again` long ago: due now

    body = client.get("/api/practice/summary").json()
    assert body["topics"] == [
        {
            "subject_id": subject,
            "subject_name": "Matemáticas",
            "topic_id": topic,
            "topic_title": "Derivadas",
            "due": 1,
            "new": 2,
            "next_due": None,
        }
    ]
    assert body["totals"] == {"due": 1, "new": 2, "topics": 1}
    assert client.get("/api/practice/summary", params={"new_limit": 1}).json()["totals"]["new"] == 1
    assert client.get("/api/practice/summary", params={"new_limit": 500}).status_code == 422


def test_several_topics_sorted_by_due_and_suspended_items_left_out(tmp_vault: Vault) -> None:
    maths = create_subject(tmp_vault, "Matemáticas").slug
    physics = create_subject(tmp_vault, "Física").slug
    one, one_keys = _topic(tmp_vault, maths, "Derivadas", 2)
    three, three_keys = _topic(tmp_vault, physics, "Cinemática", 4)
    none, _ = _topic(tmp_vault, maths, "Integrales", 1)
    for key in one_keys[:1]:
        _review(tmp_vault, maths, one, key)
    for key in three_keys[:3]:
        _review(tmp_vault, physics, three, key)
    path = study_log_path(tmp_vault, physics, three, "practice")
    append_jsonl(  # set aside: neither due nor new
        path,
        PracticeSuspension(time=PAST, item=three_keys[3], source="flashcards", action="suspend"),
    )

    summary = practice_summary(tmp_vault, now=NOW)
    assert [(t.topic_id, t.due, t.new) for t in summary.topics] == [
        (three, 3, 0),
        (one, 1, 1),
        (none, 0, 1),
    ]
    assert (summary.totals.due, summary.totals.new, summary.totals.topics) == (4, 2, 3)
    assert summary.warnings == []

    _review(tmp_vault, maths, none, flashcard_item_key("c00000000"), rating="good")
    later = practice_summary(tmp_vault, now=PAST + timedelta(hours=1))
    integrals = next(t for t in later.topics if t.topic_id == none)
    assert integrals.due == 0 and integrals.next_due == PAST + timedelta(days=1)


def test_an_unreadable_topic_is_skipped_with_a_warning(
    client: TestClient, tmp_vault: Vault
) -> None:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    good, _ = _topic(tmp_vault, subject, "Derivadas", 1)
    broken, _ = _topic(tmp_vault, subject, "Límites", 0)
    write_generated(tmp_vault, subject, broken, YAML_NAME, "cards: [no es un mazo")

    response = client.get("/api/practice/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [t["topic_id"] for t in body["topics"]] == [good]
    assert len(body["warnings"]) == 1 and "«Límites»" in body["warnings"][0]


def test_a_lan_client_without_a_token_gets_401(app: object) -> None:
    lan = TestClient(app, base_url=PUBLIC_URL, client=(LAN_HOST, 50000))  # type: ignore[arg-type]
    assert lan.get("/api/practice/summary").status_code == 401
