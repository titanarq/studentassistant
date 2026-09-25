"""The web search API: `GET/POST /api/subjects/{s}/topics/{t}/web-searches[...]/keep`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import ObserverSettings, ServerSettings, Settings, SourcesSettings
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_subject, create_topic, list_sources, read_source
from web_search_helpers import HITS, PAGE_TEXT, fetch_reply, search_reply

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
AppFactory = Callable[..., FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Revolución francesa").slug


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude | None, sources: SourcesSettings | None = None) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            sources=sources or SourcesSettings(transcription_enabled=False),
            llm_transport=transport,
            llm_settings=Settings(observer=ObserverSettings(enabled=False)),
        )

    return make


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _client(make_app(fake)) as client:
        yield client


def _base(topic: tuple[str, str]) -> str:
    return f"/api/subjects/{topic[0]}/topics/{topic[1]}/web-searches"


def _wait_done(client: TestClient, topic: tuple[str, str], search_id: str) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        for search in client.get(_base(topic)).json()["searches"]:
            if search["search_id"] == search_id and search["status"] != "queued":
                return search
        time.sleep(0.02)
    raise AssertionError(f"search {search_id} did not end in {WAIT_SECONDS} s")


def test_search_then_keep_a_result(
    client: TestClient, fake: FakeClaude, topic: tuple[str, str], tmp_vault: Vault
) -> None:
    assert client.get(_base(topic)).json() == {"searches": []}
    search_reply(fake)
    queued = client.post(_base(topic), json={"query": "toma de la Bastilla"})
    assert queued.status_code == 202, queued.text
    search_id = queued.json()["search_id"]
    assert queued.json()["status"] == "queued"

    search = _wait_done(client, topic, search_id)
    assert search["status"] == "done" and search["requested_by"] == "web"
    assert [r["url"] for r in search["results"]] == [HITS[0][0], HITS[1][0]]
    assert search["results"][0]["relevant"] is True and search["kept"] == []

    fetch_reply(fake, title="Toma de la Bastilla")
    kept = client.post(f"{_base(topic)}/{search_id}/results/0/keep")
    assert kept.status_code == 201, kept.text
    body = kept.json()
    assert body["source_id"] == "sources/web/001-toma-de-la-bastilla.md"
    assert body["url"] == HITS[0][0] and body["title"] == "Toma de la Bastilla"
    assert PAGE_TEXT in read_source(tmp_vault, body["vault_id"]).content.decode()
    (listed,) = client.get(_base(topic)).json()["searches"]
    assert listed["kept"][0]["source_id"] == body["source_id"]


def test_refusals(client: TestClient, fake: FakeClaude, topic: tuple[str, str]) -> None:
    assert client.post(_base(topic), json={"query": "  "}).status_code == 422
    assert client.post(_base(topic), json={}).status_code == 422
    unknown = client.post("/api/subjects/nada/topics/nada/web-searches", json={"query": "x"})
    assert unknown.status_code == 404
    missing = client.post(f"{_base(topic)}/ws-nada/results/0/keep")
    assert missing.status_code == 404 and missing.json()["detail"]

    search_reply(fake)
    search_id = client.post(_base(topic), json={"query": "bastilla"}).json()["search_id"]
    _wait_done(client, topic, search_id)
    fetch_reply(fake, HITS[1][0], media_type="application/pdf", data="JVBERi0=")
    pdf = client.post(f"{_base(topic)}/{search_id}/results/1/keep")
    assert pdf.status_code == 422 and "PDF" in pdf.json()["detail"]
    assert client.post(f"{_base(topic)}/{search_id}/results/9/keep").status_code == 404


def test_without_a_transport_searching_is_unavailable(
    make_app: AppFactory, topic: tuple[str, str], tmp_vault: Vault
) -> None:
    with _client(make_app(None)) as client:
        assert client.get(_base(topic)).json() == {"searches": []}
        refused = client.post(_base(topic), json={"query": "bastilla"})
        assert refused.status_code == 503 and "no está disponible" in refused.json()["detail"]
    with _client(make_app(FakeClaude(), SourcesSettings(web_search_enabled=False))) as client:
        assert client.post(_base(topic), json={"query": "bastilla"}).status_code == 503
    assert list_sources(tmp_vault, *topic) == []
