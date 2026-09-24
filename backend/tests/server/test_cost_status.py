"""`GET /api/cost`: the llm module's cost-cap status, read from the configured vault."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studentassistant.vault import (
    LedgerEntry,
    Vault,
    append_ledger_entry,
    create_subject,
    create_topic,
)

SESSION = "20260924-100000"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def configure(config: Path, vault: Vault, caps: str = "") -> None:
    config.write_text(f'[vault]\npath = "{vault.path}"\n\n[llm]\n{caps}\n', encoding="utf-8")


def seed(
    vault: Vault,
    topic: tuple[str, str],
    usd: float | None,
    *,
    session: str = SESSION,
    model: str = "claude-sonnet-5",
) -> None:
    entry = LedgerEntry(
        time=datetime.now(UTC),
        role="editor",
        model=model,
        input_tokens=100,
        output_tokens=10,
        estimated_usd=usd,
        subject=topic[0],
        topic=topic[1],
        session=session,
    )
    append_ledger_entry(vault, *topic, entry)


def test_no_cap_reports_the_day_total(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str], isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault)
    seed(tmp_vault, topic, 0.25)

    response = local.get("/api/cost")

    assert response.status_code == 200
    assert response.json() == {
        "session_usd": 0.0,
        "day_usd": 0.25,
        "max_usd_per_session": None,
        "max_usd_per_day": None,
        "observer_paused": False,
        "editor_needs_confirmation": False,
        "unpriced_session_calls": 0,
        "unpriced_day_calls": 0,
        "unpriced_models": [],
    }


def test_day_cap_reached_sets_both_flags(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str], isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault, "max_usd_per_day = 0.5")
    seed(tmp_vault, topic, 0.5)

    body = local.get("/api/cost").json()

    assert body["day_usd"] == 0.5
    assert body["max_usd_per_day"] == 0.5
    assert body["observer_paused"] and body["editor_needs_confirmation"]


def test_session_cap_needs_the_session_query_parameters(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str], isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault, "max_usd_per_session = 1.0")
    seed(tmp_vault, topic, 1.0)
    seed(tmp_vault, topic, 0.5, session="20260924-110000")

    unselected = local.get("/api/cost").json()
    selected = local.get(
        "/api/cost", params={"subject": topic[0], "topic": topic[1], "session": SESSION}
    ).json()

    assert unselected["session_usd"] == 0.0
    assert not unselected["observer_paused"]
    assert selected["session_usd"] == 1.0
    assert selected["day_usd"] == 1.5
    assert selected["max_usd_per_session"] == 1.0
    assert selected["observer_paused"] and selected["editor_needs_confirmation"]


def test_unpriced_calls_are_reported(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str], isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault)
    seed(tmp_vault, topic, None, model="claude-mystery-1")

    body = local.get(
        "/api/cost", params={"subject": topic[0], "topic": topic[1], "session": SESSION}
    ).json()

    assert body["unpriced_session_calls"] == 1
    assert body["unpriced_day_calls"] == 1
    assert body["unpriced_models"] == ["claude-mystery-1"]


@pytest.mark.parametrize(
    "params",
    [
        {"subject": "matematicas", "topic": "integrales"},
        {"subject": "historia", "topic": "derivadas", "session": SESSION},
    ],
)
def test_unknown_topic_is_404_in_spanish(
    local: TestClient,
    tmp_vault: Vault,
    topic: tuple[str, str],
    isolated_config: Path,
    params: dict[str, str],
) -> None:
    configure(isolated_config, tmp_vault)

    response = local.get("/api/cost", params=params)

    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_a_session_without_its_topic_is_refused(
    local: TestClient, tmp_vault: Vault, isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault)

    response = local.get("/api/cost", params={"session": SESSION})

    assert response.status_code == 422
    assert "subject" in response.json()["detail"]


def test_a_missing_vault_is_503(local: TestClient, tmp_path: Path, isolated_config: Path) -> None:
    isolated_config.write_text(f'[vault]\npath = "{tmp_path / "nowhere"}"\n', encoding="utf-8")

    response = local.get("/api/cost")

    assert response.status_code == 503
    assert response.json()["detail"] == "No se puede abrir la bóveda."


def test_unauthenticated_lan_request_is_refused(
    lan: TestClient, tmp_vault: Vault, isolated_config: Path
) -> None:
    configure(isolated_config, tmp_vault)

    response = lan.get("/api/cost")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_paired_device_is_served(
    lan: TestClient, tmp_vault: Vault, isolated_config: Path, pair_device
) -> None:
    configure(isolated_config, tmp_vault)
    token = pair_device()["token"]

    response = lan.get("/api/cost", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["day_usd"] == 0.0
