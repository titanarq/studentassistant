"""The notes versions API: `GET .../notes/versions[/diff|/{n}]`, `POST .../{n}/restore`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import GitSync, Vault, read_notes, write_notes

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    topic = make_revise_topic(tmp_vault)
    sync = GitSync(tmp_vault)
    sync.create_notes_tag(topic.subject, topic.topic, "Apuntes v1")
    write_notes(
        tmp_vault,
        topic.subject,
        topic.topic,
        topic.notes.replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ o df/dx."),
    )
    sync.create_notes_tag(topic.subject, topic.topic, "Apuntes v2")
    return topic


@pytest.fixture
def client(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> Iterator[TestClient]:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_transport=None,  # nothing here calls Claude
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _base(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/versions"


def test_list_read_diff_and_restore(client: TestClient, topic: ReviseTopic) -> None:
    listing = client.get(_base(topic))
    assert listing.status_code == 200
    body = listing.json()
    assert [(v["version"], v["current"]) for v in body["versions"]] == [(1, False), (2, True)]
    assert body["versions"][0]["message"] == "Apuntes v1"

    text = client.get(_base(topic) + "/1")
    assert text.status_code == 200 and text.json()["text"] == topic.notes

    diff = client.get(_base(topic) + "/diff", params={"from": 1, "to": 2})
    assert diff.status_code == 200
    sections = {s["key"]: s["status"] for s in diff.json()["sections"]}
    assert sections == {"": "unchanged", "definicion": "changed", "proximo-dia": "unchanged"}
    against_current = client.get(_base(topic) + "/diff", params={"from": 2})
    assert against_current.json()["identical"] is True

    restored = client.post(_base(topic) + "/1/restore")
    assert restored.status_code == 200
    assert (restored.json()["version"], restored.json()["restored_version"]) == (3, 1)
    assert read_notes(topic.vault, topic.subject, topic.topic) == topic.notes

    again = client.post(_base(topic) + "/1/restore")
    assert again.status_code == 409
    assert "ya son los de la versión 1" in again.json()["detail"]


def test_errors(client: TestClient, topic: ReviseTopic) -> None:
    assert client.get(_base(topic) + "/9").status_code == 404
    assert client.post(_base(topic) + "/9/restore").status_code == 404
    assert client.get(_base(topic) + "/diff", params={"from": 9}).status_code == 404
    assert client.get(_base(topic) + "/diff").status_code == 422
    assert client.get(_base(topic) + "/0").status_code == 422
    unknown = f"/api/subjects/{topic.subject}/topics/no-existe/notes/versions"
    assert client.get(unknown).status_code == 404
    assert client.get(unknown).json()["detail"] == "No existe ese tema en la bóveda."
