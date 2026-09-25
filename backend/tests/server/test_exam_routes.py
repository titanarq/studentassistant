"""Correcting the mock exam from the web: `GET .../exam`, `POST/GET .../exam/results`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.generators.exam import TOOL_NAME
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault
from studentassistant.vault.study import study_log_path

LOCAL_BASE_URL = "http://localhost:8765"
QUESTIONS: list[dict[str, Any]] = [
    {
        "statement": "Define la derivada de f en a.",
        "difficulty": "media",
        "points": 4,
        "solution": "Es el límite del cociente incremental.",
        "rubric": [
            {"criterion": "Escribe el cociente incremental", "points": 2},
            {"criterion": "Toma el límite cuando h → 0", "points": 2},
        ],
        "anchors": ["definicion"],
    },
    {
        "statement": "¿Qué regla se verá el próximo día?",
        "difficulty": "baja",
        "points": 6,
        "solution": "La regla de la cadena.",
        "rubric": [{"criterion": "Nombra la regla de la cadena", "points": 6}],
        "anchors": ["proximo-dia"],
    },
]


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


def _generate(client: TestClient, fake: FakeClaude, base: str) -> None:
    fake.reply_tool(TOOL_NAME, {"instructions": "", "exercises": [], "exam": QUESTIONS})
    generated = client.post(
        f"{base}/generated/examen", json={"options": {"exercises": 0, "questions": 2}}
    )
    assert generated.status_code == 200, generated.text


def test_generate_correct_and_read_results(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    base = _topic_base(topic)
    missing = client.get(f"{base}/exam")
    assert missing.status_code == 404 and "Todavía no hay examen" in missing.json()["detail"]

    _generate(client, fake, base)
    stored = client.get(f"{base}/exam").json()
    assert [q["id"] for q in stored["exam"]["questions"]] == ["p1", "p2"]
    assert stored["exam"]["questions"][0]["rubric"][0]["points"] == 2
    assert stored["stale"] is False
    attempt = {
        "built_at": stored["built_at"],
        "questions": [
            {"question": "p1", "awarded": [2, 1]},
            {"question": "p2", "awarded": [4.5]},
        ],
    }
    recorded = client.post(f"{base}/exam/results", json=attempt)
    assert recorded.status_code == 200, recorded.text
    body = recorded.json()
    assert (body["score"], body["total"], body["percentage"]) == (7.5, 10, 75.0)
    assert study_log_path(topic.vault, topic.subject, topic.topic, "exam-results").is_file()

    history = client.get(f"{base}/exam/results").json()
    assert [entry["score"] for entry in history] == [7.5]


def test_refusals(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> None:
    base = _topic_base(topic)
    no_exam = client.post(
        f"{base}/exam/results", json={"built_at": "2026-09-25T18:00:00Z", "questions": []}
    )
    assert no_exam.status_code == 404
    unknown = client.get(f"/api/subjects/{topic.subject}/topics/no-existe/exam")
    assert unknown.status_code == 404 and "No existe ese tema" in unknown.json()["detail"]

    _generate(client, fake, base)
    built_at = client.get(f"{base}/exam").json()["built_at"]
    changed = client.post(
        f"{base}/exam/results", json={"built_at": "2026-09-25T18:00:00Z", "questions": []}
    )
    assert changed.status_code == 409 and "ha cambiado" in changed.json()["detail"]
    bad = client.post(
        f"{base}/exam/results",
        json={"built_at": built_at, "questions": [{"question": "p9", "awarded": [1]}]},
    )
    assert bad.status_code == 422 and "p9" in bad.json()["detail"]
    over = client.post(
        f"{base}/exam/results",
        json={"built_at": built_at, "questions": [{"question": "p2", "awarded": [7]}]},
    )
    assert over.status_code == 422 and "entre 0 y 6" in over.json()["detail"]
    assert client.get(f"{base}/exam/results").json() == []
