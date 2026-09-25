"""`GET /api/subjects/{s}/topics/{t}/cost`: a topic's ledger summed per session and without one."""

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
    start_session,
)

GONE_SESSION = "20260920-080000"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


@pytest.fixture(autouse=True)
def configured(isolated_config: Path, tmp_vault: Vault) -> None:
    isolated_config.write_text(f'[vault]\npath = "{tmp_vault.path}"\n', encoding="utf-8")


def seed(
    vault: Vault,
    topic: tuple[str, str],
    usd: float | None,
    *,
    session: str | None,
    role: str = "observer",
    time: datetime | None = None,
) -> None:
    entry = LedgerEntry(
        time=time or datetime.now(UTC),
        role=role,
        model="claude-sonnet-5",
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=5,
        cache_write_tokens=1,
        estimated_usd=usd,
        subject=topic[0],
        topic=topic[1],
        session=session,
    )
    append_ledger_entry(vault, *topic, entry)


def path(topic: tuple[str, str]) -> str:
    return f"/api/subjects/{topic[0]}/topics/{topic[1]}/cost"


def test_sums_per_session_and_without_one(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    meta = start_session(tmp_vault, *topic, "pc", "1.1").meta
    seed(tmp_vault, topic, 0.25, session=meta.id)
    seed(tmp_vault, topic, 0.5, session=meta.id)
    seed(tmp_vault, topic, 1.0, session=None, role="editor")
    seed(tmp_vault, topic, None, session=None, role="generator")
    first = datetime(2026, 9, 20, 8, 1, tzinfo=UTC)
    seed(tmp_vault, topic, 0.125, session=GONE_SESSION, time=first)

    response = local.get(path(topic))

    assert response.status_code == 200
    body = response.json()
    assert body["subject_id"] == topic[0]
    assert body["topic_id"] == topic[1]
    assert body["total"] == {
        "usd": 1.875,
        "tokens": 580,
        "input_tokens": 500,
        "output_tokens": 50,
        "cache_read_tokens": 25,
        "cache_write_tokens": 5,
        "calls": 5,
        "unpriced_calls": 1,
    }
    gone, live = body["sessions"]
    assert gone["session_id"] == GONE_SESSION
    assert gone["started_at_ms"] == int(first.timestamp() * 1000)
    assert gone["usd"] == 0.125 and gone["calls"] == 1
    assert live["session_id"] == meta.id
    assert live["started_at_ms"] == int(meta.started_at.timestamp() * 1000)
    assert live["usd"] == 0.75
    assert live["tokens"] == 232
    assert live["calls"] == 2
    assert live["unpriced_calls"] == 0
    assert body["no_session"]["usd"] == 1.0
    assert body["no_session"]["calls"] == 2
    assert body["no_session"]["unpriced_calls"] == 1


def test_a_topic_without_spend_lists_its_sessions_at_zero(
    local: TestClient, tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    meta = start_session(tmp_vault, *topic, "pc", "1.1").meta

    body = local.get(path(topic)).json()

    assert body["total"]["usd"] == 0.0
    assert body["total"]["calls"] == 0
    assert [row["session_id"] for row in body["sessions"]] == [meta.id]
    assert body["sessions"][0]["calls"] == 0
    assert body["no_session"]["calls"] == 0


@pytest.mark.parametrize(
    "topic_path", ["matematicas/topics/integrales", "historia/topics/derivadas"]
)
def test_unknown_topic_is_404_in_spanish(
    local: TestClient, topic: tuple[str, str], topic_path: str
) -> None:
    response = local.get(f"/api/subjects/{topic_path}/cost")

    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_a_malformed_id_is_422(local: TestClient) -> None:
    assert local.get("/api/subjects/Mal%20Id/topics/derivadas/cost").status_code == 422


def test_a_missing_vault_is_503(local: TestClient, tmp_path: Path, isolated_config: Path) -> None:
    isolated_config.write_text(f'[vault]\npath = "{tmp_path / "nowhere"}"\n', encoding="utf-8")

    response = local.get("/api/subjects/matematicas/topics/derivadas/cost")

    assert response.status_code == 503
    assert response.json()["detail"] == "No se puede abrir la bóveda."


def test_unauthenticated_lan_request_is_refused(lan: TestClient, topic: tuple[str, str]) -> None:
    response = lan.get(path(topic))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_paired_device_is_served(lan: TestClient, topic: tuple[str, str], pair_device) -> None:
    token = pair_device()["token"]

    response = lan.get(path(topic), headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["sessions"] == []
