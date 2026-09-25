"""Practising with spaced repetition from the web: `GET ./practice`, `POST ./practice/reviews`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quiz_replies import MULTIPLE_CHOICE, reply_quiz
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.generators.flashcards import TOOL_NAME as FLASHCARDS_TOOL
from studentassistant.generators.practice import quiz_item_key
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault
from studentassistant.vault.study import study_log_path

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, fake: FakeClaude
) -> Iterator[TestClient]:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def test_practise_flashcards_and_quiz(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    base = f"/api/subjects/{topic.subject}/topics/{topic.topic}"
    empty = client.get(f"{base}/practice")
    assert empty.status_code == 200 and empty.json()["queue"] == []
    assert "Todavía no hay" in empty.json()["warnings"][0]

    fake.reply_tool(FLASHCARDS_TOOL, {"cards": [{"front": "¿Qué es?", "back": "Un límite."}]})
    assert client.post(f"{base}/generated/flashcards", json={}).status_code == 200
    reply_quiz(fake)
    assert client.post(f"{base}/generated/quiz", json={"options": {"size": 3}}).status_code == 200

    queue = client.get(f"{base}/practice", params={"new_limit": 3}).json()
    assert [q["item"]["source"] for q in queue["queue"]] == ["flashcards", "quiz", "quiz"]
    assert queue["counts"]["unseen"] == 4 and queue["counts"]["new"] == 3
    card = queue["queue"][0]["item"]["key"]

    reviewed = client.post(f"{base}/practice/reviews", json={"item": card, "rating": "good"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["state"]["interval_days"] == 1.0
    question = quiz_item_key(MULTIPLE_CHOICE["question"])
    graded = client.post(f"{base}/practice/reviews", json={"item": question, "given": "Una suma"})
    assert graded.json()["review"]["correct"] is False
    assert graded.json()["review"]["rating"] == "again"
    assert study_log_path(topic.vault, topic.subject, topic.topic, "practice").is_file()

    after = client.get(f"{base}/practice").json()
    assert after["counts"]["learned"] == 2 and after["next_due"] is not None

    missing = client.post(f"{base}/practice/reviews", json={"item": "quiz:nada", "rating": "good"})
    assert missing.status_code == 404 and "ya no está" in missing.json()["detail"]
    unrated = client.post(f"{base}/practice/reviews", json={"item": card})
    assert unrated.status_code == 422 and "otra vez" in unrated.json()["detail"]
    assert client.get(f"{base}/practice", params={"new_limit": 500}).status_code == 422


def test_unknown_topic(client: TestClient, topic: ReviseTopic) -> None:
    response = client.get(f"/api/subjects/{topic.subject}/topics/nada/practice")
    assert response.status_code == 404
    review = client.post(
        f"/api/subjects/{topic.subject}/topics/nada/practice/reviews",
        json={"item": "quiz:x", "rating": "good"},
    )
    assert review.status_code == 404
