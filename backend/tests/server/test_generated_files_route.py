"""`GET .../topics/{t}/generated/files/{name}`: downloading generated material from the web."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, write_generated

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def client(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> Iterator[TestClient]:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _files(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/generated/files"


def test_downloads_a_generated_file(client: TestClient, topic: ReviseTopic) -> None:
    write_generated(topic.vault, topic.subject, topic.topic, "flashcards.apkg", b"PK\x03\x04deck")
    write_generated(topic.vault, topic.subject, topic.topic, "slides/slides.md", "# Hola\n")

    response = client.get(f"{_files(topic)}/flashcards.apkg")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04deck"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{topic.topic}-flashcards.apkg"'
    )

    nested = client.get(f"{_files(topic)}/slides/slides.md")
    assert nested.status_code == 200 and nested.text == "# Hola\n"
    assert nested.headers["content-type"].startswith("text/markdown")


def test_csv_is_served_as_utf8_text(client: TestClient, topic: ReviseTopic) -> None:
    write_generated(topic.vault, topic.subject, topic.topic, "flashcards.csv", "id,anverso\n")
    response = client.get(f"{_files(topic)}/flashcards.csv")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"


@pytest.mark.parametrize("name", ["flashcards.apkg", ".oculto", "a/.b", "quiz..yaml/x"])
def test_a_missing_or_bad_name_is_404(client: TestClient, topic: ReviseTopic, name: str) -> None:
    response = client.get(f"{_files(topic)}/{name}")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese archivo en el material generado del tema."


def test_an_unknown_topic_is_404(client: TestClient, topic: ReviseTopic) -> None:
    response = client.get(f"/api/subjects/{topic.subject}/topics/otro/generated/files/a.csv")
    assert response.status_code == 404
