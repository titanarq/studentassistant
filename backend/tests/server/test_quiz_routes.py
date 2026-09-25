"""Taking the quiz from the web: `GET .../quiz`, `POST/GET .../quiz/results`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quiz_replies import reply_quiz
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
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


def _topic_base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


def test_generate_take_and_read_results(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    base = _topic_base(topic)
    missing = client.get(f"{base}/quiz")
    assert missing.status_code == 404 and "Todavía no hay quiz" in missing.json()["detail"]

    reply_quiz(fake)
    generated = client.post(f"{base}/generated/quiz", json={"options": {"size": 3}})
    assert generated.status_code == 200, generated.text

    stored = client.get(f"{base}/quiz").json()
    assert [q["id"] for q in stored["quiz"]["questions"]] == ["q1", "q2", "q3"]
    attempt = {
        "built_at": stored["built_at"],
        "answers": [
            {"question": "q1", "given": "Un límite"},
            {"question": "q3", "given": "cadena", "self_assessed": False},
        ],
        "duration_seconds": 30,
    }
    recorded = client.post(f"{base}/quiz/results", json=attempt)
    assert recorded.status_code == 200, recorded.text
    assert (recorded.json()["correct"], recorded.json()["total"]) == (1, 3)
    assert study_log_path(topic.vault, topic.subject, topic.topic, "quiz-results").is_file()

    history = client.get(f"{base}/quiz/results").json()
    assert [entry["correct"] for entry in history] == [1]


def test_refusals(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> None:
    base = _topic_base(topic)
    no_quiz = client.post(
        f"{base}/quiz/results", json={"built_at": "2026-09-25T18:00:00Z", "answers": []}
    )
    assert no_quiz.status_code == 404
    unknown = client.get(f"/api/subjects/{topic.subject}/topics/no-existe/quiz")
    assert unknown.status_code == 404 and "No existe ese tema" in unknown.json()["detail"]

    reply_quiz(fake)
    assert client.post(f"{base}/generated/quiz").status_code == 200
    built_at = client.get(f"{base}/quiz").json()["built_at"]
    changed = client.post(
        f"{base}/quiz/results", json={"built_at": "2026-09-25T18:00:00Z", "answers": []}
    )
    assert changed.status_code == 409 and "ha cambiado" in changed.json()["detail"]
    bad = client.post(
        f"{base}/quiz/results", json={"built_at": built_at, "answers": [{"question": "q9"}]}
    )
    assert bad.status_code == 422 and "q9" in bad.json()["detail"]
    assert client.get(f"{base}/quiz/results").json() == []


def test_partial_attempt(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> None:
    base = _topic_base(topic)
    reply_quiz(fake)
    assert client.post(f"{base}/generated/quiz").status_code == 200
    built_at = client.get(f"{base}/quiz").json()["built_at"]
    retake = {
        "built_at": built_at,
        "questions": ["q2"],
        "answers": [{"question": "q2", "given": "Verdadero"}],
    }
    recorded = client.post(f"{base}/quiz/results", json=retake)
    assert recorded.status_code == 200
    body = recorded.json()
    assert (body["total"], body["correct"], body["questions"]) == (1, 1, ["q2"])
    unknown = client.post(
        f"{base}/quiz/results", json={**retake, "questions": ["q9"], "answers": []}
    )
    assert unknown.status_code == 422 and "q9" in unknown.json()["detail"]
    assert [r["questions"] for r in client.get(f"{base}/quiz/results").json()] == [["q2"]]
