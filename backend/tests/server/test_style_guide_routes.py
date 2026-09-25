"""The subject style guide API: `GET/PUT .../style-guide` and `POST .../style-guide/rules`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, create_subject, get_subject

LOCAL_BASE_URL = "http://localhost:8765"


@pytest.fixture
def subject(tmp_vault: Vault) -> str:
    return create_subject(tmp_vault, "Historia", style_guide="Fechas en negrita.").slug


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


def test_list_confirm_and_edit_the_rules(
    client: TestClient, tmp_vault: Vault, subject: str
) -> None:
    base = f"/api/subjects/{subject}/style-guide"

    assert client.get(base).json() == {
        "subject": subject,
        "rules": ["Fechas en negrita."],
        "added": [],
        "commit": None,
    }

    added = client.post(f"{base}/rules", json={"rules": ["Usa tablas para comparar."]})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["rules"] == ["Fechas en negrita.", "Usa tablas para comparar."]
    assert body["added"] == ["Usa tablas para comparar."] and body["commit"]

    edited = client.put(base, json={"rules": ["Usa tablas para comparar."]})
    assert edited.status_code == 200 and edited.json()["rules"] == ["Usa tablas para comparar."]
    assert get_subject(tmp_vault, subject).subject.style_guide == "- Usa tablas para comparar.\n"


def test_errors(client: TestClient, subject: str) -> None:
    assert client.get("/api/subjects/no-existe/style-guide").status_code == 404
    unknown = client.post("/api/subjects/no-existe/style-guide/rules", json={"rules": ["Tablas."]})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "No existe esa asignatura en la bóveda."

    empty = client.put(f"/api/subjects/{subject}/style-guide", json={"rules": [" "]})
    assert empty.status_code == 422 and "vacía" in empty.json()["detail"]
    assert client.get(f"/api/subjects/{subject}/style-guide").json()["rules"] == [
        "Fechas en negrita."
    ]
