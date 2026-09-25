"""The study materials API: `GET /api/generators`, `GET/POST .../topics/{t}/generated[/{kind}]`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from material_generators import DEFAULT_POINTS, KIND, points_registry, reply_points
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import LlmSettings, ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude, LLMServerError
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_topic, write_notes

LOCAL_BASE_URL = "http://localhost:8765"
AppFactory = Callable[..., FastAPI]


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude | None, llm: LlmSettings | None = None) -> FastAPI:
        app = create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False), llm=llm or LlmSettings()
            ),
        )
        app.state.generators = points_registry()
        return app

    return make


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _local(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _local(make_app(fake)) as client:
        yield client


def _base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/generated"


def test_lists_the_registered_generators(client: TestClient) -> None:
    response = client.get("/api/generators")
    assert response.status_code == 200
    [info] = response.json()
    assert info["kind"] == KIND and info["title"] == "Puntos" and info["version"] == 1
    assert set(info["options_schema"]["properties"]) == {"size", "split"}


def test_generate_then_status_then_stale(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    before = client.get(_base(topic)).json()
    assert before["has_notes"] and before["artifacts"][0]["generated"] is False

    reply_points(fake, *DEFAULT_POINTS)
    response = client.post(f"{_base(topic)}/{KIND}", json={"options": {"size": 2}})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == KIND and body["items"] == 2 and body["commit"]
    assert fake.requests[0].role == "generator"
    [artifact] = client.get(_base(topic)).json()["artifacts"]
    assert artifact["generated"] and not artifact["stale"]
    assert artifact["meta"]["options"] == {"size": 2, "split": False}

    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nOtro.[^p1]\n")
    [artifact] = client.get(_base(topic)).json()["artifacts"]
    assert artifact["stale"] and "han cambiado" in artifact["stale_reason"]


def test_without_a_transport_status_works_but_generating_is_503(
    make_app: AppFactory, topic: ReviseTopic
) -> None:
    with _local(make_app(None)) as client:
        assert client.get(_base(topic)).status_code == 200
        response = client.post(f"{_base(topic)}/{KIND}")
    assert response.status_code == 503
    assert "no está disponible" in response.json()["detail"]


def test_refusals(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> None:
    unknown_kind = client.post(f"{_base(topic)}/otro")
    assert unknown_kind.status_code == 404 and "otro" in unknown_kind.json()["detail"]
    unknown_topic = client.get(f"/api/subjects/{topic.subject}/topics/no-existe/generated")
    assert unknown_topic.status_code == 404
    bad_options = client.post(f"{_base(topic)}/{KIND}", json={"options": {"size": 0}})
    assert bad_options.status_code == 422 and "size" in bad_options.json()["detail"]
    empty = create_topic(topic.vault, topic.subject, "Integrales").slug
    no_notes = client.post(f"/api/subjects/{topic.subject}/topics/{empty}/generated/{KIND}")
    assert no_notes.status_code == 409 and "aún no tiene apuntes" in no_notes.json()["detail"]
    assert fake.requests == []


def test_a_claude_failure_is_502(client: TestClient, fake: FakeClaude, topic: ReviseTopic) -> None:
    for _ in range(4):
        fake.fail(LLMServerError("boom", status_code=500))
    response = client.post(f"{_base(topic)}/{KIND}")
    assert response.status_code == 502
    assert client.get(_base(topic)).json()["artifacts"][0]["generated"] is False


def test_a_reached_cap_is_409_until_confirmed(
    make_app: AppFactory, fake: FakeClaude, topic: ReviseTopic
) -> None:
    reply_points(fake, *DEFAULT_POINTS)
    with _local(make_app(fake, LlmSettings(max_usd_per_day=0))) as client:
        refused = client.post(f"{_base(topic)}/{KIND}")
        assert refused.status_code == 409
        assert "Confirma para generar el material" in refused.json()["detail"]
        confirmed = client.post(f"{_base(topic)}/{KIND}", json={"confirm_over_cap": True})
    assert confirmed.status_code == 200, confirmed.text
