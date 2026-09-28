"""`GET /api/feedback[?status=]`: the vault's app feedback inbox, authenticated like `/api`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import (
    FeedbackContext,
    Vault,
    add_feedback,
    feedback_path,
    set_feedback_status,
)

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def app(server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> FastAPI:
    """The conftest app, over `tmp_vault` (the routes read it through the session service)."""
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )


def seed(vault: Vault) -> tuple[str, str]:
    """Two items (a triaged bug, then a new mejora); their ids, in that order."""
    bug = add_feedback(
        vault,
        "bug",
        "El micro se corta",
        "Al hablar mucho rato el micro se corta.",
        FeedbackContext(subject="mates", topic="derivadas", route="workspace", mode="construir"),
        clock=lambda: T0,
    )
    mejora = add_feedback(
        vault, "mejora", "Modo oscuro", "Quiero un modo oscuro.", clock=lambda: T0
    )
    set_feedback_status(vault, bug.id, "triado", 812, clock=lambda: T0)
    return bug.id, mejora.id


def test_lists_every_item_folded_oldest_first(local: TestClient, tmp_vault: Vault) -> None:
    ids = seed(tmp_vault)

    response = local.get("/api/feedback")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == list(ids)
    assert items[0]["status"] == "triado" and items[0]["issue"] == 812
    assert items[0]["kind"] == "bug" and items[0]["context"]["topic"] == "derivadas"
    assert items[1]["status"] == "nuevo" and items[1]["issue"] is None


def test_status_filters_the_items(local: TestClient, tmp_vault: Vault) -> None:
    _, mejora = seed(tmp_vault)

    nuevo = local.get("/api/feedback", params={"status": "nuevo"})
    descartado = local.get("/api/feedback", params={"status": "descartado"})

    assert [item["id"] for item in nuevo.json()["items"]] == [mejora]
    assert descartado.json() == {"items": []}


def test_an_empty_inbox_is_an_empty_list(local: TestClient, tmp_vault: Vault) -> None:

    response = local.get("/api/feedback")

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_an_unknown_status_is_422(local: TestClient, tmp_vault: Vault) -> None:

    assert local.get("/api/feedback", params={"status": "hecho"}).status_code == 422


def test_a_corrupt_inbox_is_500(local: TestClient, tmp_vault: Vault) -> None:
    path = feedback_path(tmp_vault)
    path.parent.mkdir()
    path.write_text('{"record": "otro"}\n', encoding="utf-8")

    response = local.get("/api/feedback")

    assert response.status_code == 500
    assert response.json()["detail"] == "No se puede leer el buzón de comentarios de la bóveda."


def test_a_missing_vault_is_503(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, isolated_config: Path
) -> None:
    isolated_config.write_text(f'[vault]\npath = "{tmp_path / "nowhere"}"\n', encoding="utf-8")
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        llm_settings=Settings(
            vault={"path": tmp_path / "nowhere"}, observer=ObserverSettings(enabled=False)
        ),
    )

    client = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))

    response = client.get("/api/feedback")

    assert response.status_code == 503
    assert response.json()["detail"] == "No se puede abrir la bóveda."


def test_unauthenticated_lan_request_is_refused(lan: TestClient, tmp_vault: Vault) -> None:
    seed(tmp_vault)

    response = lan.get("/api/feedback")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "items" not in response.json()


def test_a_wrong_token_is_refused(lan: TestClient, tmp_vault: Vault) -> None:

    response = lan.get("/api/feedback", headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401


def test_paired_device_is_served(lan: TestClient, tmp_vault: Vault, pair_device) -> None:
    seed(tmp_vault)
    token = pair_device()["token"]

    response = lan.get("/api/feedback", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
