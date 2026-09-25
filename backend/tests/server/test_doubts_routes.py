"""The doubts API for the web's pending panel: `GET/POST /api/subjects/{s}/topics/{t}/doubts...`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doubts_topic import DoubtsTopic, make_doubts_topic, review_reply
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.editor.doubts import DECISION_TOOL, REVIEW_TOOL
from studentassistant.llm import FakeClaude, LLMServerError
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, read_notes, start_session

LOCAL_BASE_URL = "http://localhost:8765"
AppFactory = Callable[[FakeClaude | None], FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> DoubtsTopic:
    return make_doubts_topic(tmp_vault)


def _base(topic: DoubtsTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/doubts"


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude | None) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            llm_transport=transport,
            llm_settings=Settings(observer=ObserverSettings(enabled=False)),
        )

    return make


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with TestClient(make_app(fake), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def test_review_then_answer_then_dismiss(
    client: TestClient, fake: FakeClaude, topic: DoubtsTopic
) -> None:
    queue = client.get(_base(topic)).json()
    assert queue["open_count"] == 3 and queue["current"] == "p-1"
    assert all(item["question"] is None for item in queue["items"])

    fake.reply_tool(REVIEW_TOOL, review_reply(topic))
    review = client.post(f"{_base(topic)}/review")
    assert review.status_code == 200, review.text
    assert review.json()["auto_resolved"] == ["p-1"] and review.json()["asked"] == ["p-4", "p-5"]

    queue = client.get(_base(topic)).json()
    assert queue["current"] == "p-4"
    first = queue["items"][0]
    assert first["item"]["id"] == "p-4"
    assert first["question"]["suggestions"] == ["incremental", "diferencial"]

    fake.reply_tool(DECISION_TOOL, {"resolution": "Pone «incremental».", "edits": []})
    answer = client.post(f"{_base(topic)}/p-4/answer", json={"suggestion": 1})
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "resolved" and answer.json()["notes_changed"] is False

    dismiss = client.post(f"{_base(topic)}/p-5/dismiss")
    assert dismiss.status_code == 200 and dismiss.json()["status"] == "dismissed"
    assert client.get(_base(topic)).json()["open_count"] == 0
    # The read side of #80 sees the same fold.
    pending = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/pending").json()
    assert pending["open_count"] == 0
    assert {i["id"]: i["status"] for i in pending["items"]}["p-5"] == "dismissed"

    again = client.post(f"{_base(topic)}/p-5/dismiss")
    assert again.status_code == 409
    assert again.json() == {"detail": "Esa duda ya está cerrada.", "code": "doubt_closed"}


def test_errors(client: TestClient, fake: FakeClaude, topic: DoubtsTopic) -> None:
    assert client.post(f"{_base(topic)}/p-99/dismiss").status_code == 404
    assert client.get("/api/subjects/nada/topics/nada/doubts").status_code == 404
    bad = client.post(f"{_base(topic)}/p-4/answer", json={"suggestion": 1})
    assert bad.status_code == 422 and "sugerida" in bad.json()["detail"]
    assert "code" not in bad.json()  # only the refusals a client branches on carry one
    assert client.post(f"{_base(topic)}/p-4/answer", json={"suggestion": 9}).status_code == 422

    for _ in range(4):  # every attempt of the client's retries
        fake.fail(LLMServerError("overloaded", status_code=529, retry_after=0))
    failed = client.post(f"{_base(topic)}/review")
    assert failed.status_code == 502
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes


def test_an_unended_session_blocks_the_doubts(client: TestClient, topic: DoubtsTopic) -> None:
    start_session(topic.vault, topic.subject, topic.topic, host="pc", protocol_version="1.1")
    response = client.post(f"{_base(topic)}/p-4/dismiss")
    assert response.status_code == 409 and "sesión sin terminar" in response.json()["detail"]
    assert response.json()["code"] == "session_open"


def test_without_a_transport_only_listing_and_dismissing_work(
    make_app: AppFactory, topic: DoubtsTopic
) -> None:
    client = TestClient(make_app(None), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))
    assert client.post(f"{_base(topic)}/review").status_code == 503
    assert client.post(f"{_base(topic)}/p-4/answer", json={"answer": "x"}).status_code == 503
    assert client.post(f"{_base(topic)}/p-4/dismiss").status_code == 200
    assert client.get(_base(topic)).json()["open_count"] == 2
