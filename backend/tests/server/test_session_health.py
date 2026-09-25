"""Per-session health (#262): observer, transcription and push failures, and the health route.

The observer is driven with `FakeClaude` scripted to fail; the push failure comes from a `tmp_vault`
whose `origin` does not exist. Every wait is bounded, so nothing here can hang.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import (
    LlmSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
    VaultGitSettings,
)
from studentassistant.llm import FakeClaude, LLMAPIError, RefusalError
from studentassistant.observer.live import (
    CALL_FAILED_EVENT_KIND,
    STATUS_EVENT_KIND,
    TOOL_NAME,
    ObserverLoop,
    default_client_factory,
)
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.app import create_app
from studentassistant.server.bus import BusEvent, SessionBus
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.session_health import (
    SESSION_ENDED_KIND,
    TRANSCRIPTION_FAILED_KIND,
    SessionHealth,
)
from studentassistant.vault import (
    GitSync,
    PushFailure,
    Session,
    SyncStatus,
    Vault,
    create_subject,
    create_topic,
    start_session,
)

WAIT = 5.0


def event(kind: str, payload: dict[str, Any], session_id: str = "s1") -> BusEvent:
    return BusEvent(
        session_id=session_id,
        subject_id="biologia",
        topic_id="la-celula",
        kind=kind,
        origin="observer",
        t=0,
        payload=payload,
    )


# -- the tracker --------------------------------------------------------------------------------


def test_a_healthy_session_has_every_count_at_zero() -> None:
    health = SessionHealth(SessionBus())

    summary = health.summary("s1", SyncStatus())

    assert summary.model_dump() == {
        "session_id": "s1",
        "ok": True,
        "observer": {"count": 0, "message": None},
        "observer_paused": False,
        "observer_paused_message": None,
        "transcription": {"count": 0, "message": None},
        "push": {"count": 0, "message": None},
    }


def test_failures_are_counted_per_session_with_the_last_spanish_message() -> None:
    health = SessionHealth(SessionBus())
    health.record(event(CALL_FAILED_EVENT_KIND, {"kind": "invalid", "reason": "bad json"}))
    health.record(event(CALL_FAILED_EVENT_KIND, {"kind": "unavailable", "reason": "529"}))
    health.record(event(TRANSCRIPTION_FAILED_KIND, {"capture_id": "c1", "reason": "refused"}))
    health.record(event(CALL_FAILED_EVENT_KIND, {"kind": "error"}, session_id="other"))

    summary = health.summary("s1", SyncStatus())

    assert not summary.ok
    assert summary.observer.count == 2
    assert summary.observer.message == "Claude no responde (sin conexión o saturado)"
    assert summary.transcription.count == 1
    assert summary.transcription.message == "Claude ha rechazado la página"
    assert health.summary("other", SyncStatus()).observer.count == 1


def test_a_cost_cap_pause_shows_until_the_observer_runs_again() -> None:
    health = SessionHealth(SessionBus())
    health.record(event(STATUS_EVENT_KIND, {"status": "paused", "cap": "day"}))

    paused = health.summary("s1", SyncStatus())
    assert paused.observer_paused and not paused.ok
    assert paused.observer_paused_message == "se ha alcanzado el límite de gasto del día"

    health.record(event(STATUS_EVENT_KIND, {"status": "running", "reason": ""}))
    assert health.summary("s1", SyncStatus()).ok


def test_the_push_streak_comes_from_the_sync_status() -> None:
    health = SessionHealth(SessionBus())
    failing = SyncStatus(
        last_push_failure=PushFailure(kind="auth", message="denied", at=_now()),
        consecutive_push_failures=3,
    )

    summary = health.summary("s1", failing)

    assert summary.push.count == 3
    assert summary.push.message == "GitHub no acepta las credenciales de este equipo"
    assert not summary.ok


def test_an_ended_session_is_forgotten() -> None:
    health = SessionHealth(SessionBus())
    health.record(event(TRANSCRIPTION_FAILED_KIND, {"reason": "error"}))
    health.record(event(SESSION_ENDED_KIND, {}))

    assert health.summary("s1", SyncStatus()).ok


def _now() -> datetime:
    return datetime.now(UTC)


# -- fed from the bus by a failing observer -----------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault) -> Session:
    subject = create_subject(tmp_vault, "Biología")
    topic = create_topic(tmp_vault, subject.slug, "La célula")
    return start_session(tmp_vault, subject.slug, topic.slug, "pc", PROTOCOL_VERSION)


async def _segment(bus: SessionBus, session: Session, n: int) -> None:
    start = n * 10_000
    await bus.publish(
        session.id,
        "transcript.final",
        "stt",
        {
            "segment_id": f"seg-{n}",
            "session_start_ms": start,
            "session_end_ms": start + 2_000,
            "text": f"frase {n}",
            "provider": "fake",
        },
        t=start,
    )


@pytest.mark.anyio
async def test_failing_observer_calls_and_transcriptions_reach_the_summary(
    session: Session,
) -> None:
    bus = SessionBus()
    bus.attach(session)
    health = SessionHealth(bus)
    fake = FakeClaude()
    fake.fail(LLMAPIError("bad request", status_code=400))
    fake.fail(RefusalError("declined"))
    fake.reply_tool(TOOL_NAME, {"ops": []})
    settings = Settings(
        llm=LlmSettings(), observer=ObserverSettings(batch_segments=1, batch_speech_seconds=1000)
    )
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
    )
    loop.start()
    try:
        for n in (1, 2):
            await _segment(bus, session, n)
            await asyncio.wait_for(loop.wait_idle(session.id), WAIT)
        await bus.publish(
            session.id,
            TRANSCRIPTION_FAILED_KIND,
            "observer",
            {"capture_id": "c1", "reason": "error", "message": "boom", "attempts": 3},
        )

        failing = health.summary(session.id, SyncStatus())
        assert failing.observer.count == 2
        assert failing.observer.message == "Claude ha rechazado la petición"
        assert failing.transcription.count == 1
        assert failing.transcription.message == "Claude no ha podido transcribir la página"
        assert not failing.observer_paused

        await _segment(bus, session, 3)  # the observer recovers; the counts stay
        await asyncio.wait_for(loop.wait_idle(session.id), WAIT)
        assert health.summary(session.id, SyncStatus()).observer.count == 2
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)
        health.close()


@pytest.mark.anyio
async def test_a_cost_capped_observer_is_paused_not_failing(session: Session) -> None:
    bus = SessionBus()
    bus.attach(session)
    health = SessionHealth(bus)
    settings = Settings(
        llm=LlmSettings(max_usd_per_session=0.0),
        observer=ObserverSettings(batch_segments=1, batch_speech_seconds=1000),
    )
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, FakeClaude()),
    )
    loop.start()
    try:
        await _segment(bus, session, 1)
        await asyncio.wait_for(loop.wait_idle(session.id), WAIT)

        summary = health.summary(session.id, SyncStatus())
        assert summary.observer_paused
        assert summary.observer_paused_message == "se ha alcanzado el límite de gasto de la sesión"
        assert summary.observer.count == 0
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)
        health.close()


# -- the route ----------------------------------------------------------------------------------


def _bearer(pair: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair()['token']}"}


def _start(lan: TestClient, headers: dict[str, str]) -> str:
    lan.post("/api/subjects", json={"name": "Física"}, headers=headers)
    lan.post("/api/subjects/fisica/topics", json={"name": "Cinemática"}, headers=headers)
    started = lan.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1_000},
        headers=headers,
    )
    assert started.status_code == 201
    return started.json()["session_id"]


@pytest.fixture
def app(server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=tmp_vault
    )


def test_the_route_answers_a_healthy_session_with_zeros(
    app: FastAPI, lan: TestClient, pair_device: Any
) -> None:
    headers = _bearer(pair_device)
    session_id = _start(lan, headers)

    response = lan.get(f"/api/sessions/{session_id}/health", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["session_id"] == session_id
    assert body["observer"]["count"] == body["transcription"]["count"] == 0
    assert body["push"]["count"] == 0


def test_the_route_reports_what_the_tracker_counted(
    app: FastAPI, lan: TestClient, pair_device: Any
) -> None:
    headers = _bearer(pair_device)
    session_id = _start(lan, headers)
    health: SessionHealth = app.state.health
    for _ in range(3):
        health.record(event(CALL_FAILED_EVENT_KIND, {"kind": "refused"}, session_id=session_id))

    body = lan.get(f"/api/sessions/{session_id}/health", headers=headers).json()

    assert body["ok"] is False
    assert body["observer"] == {"count": 3, "message": "Claude ha rechazado la petición"}


def test_an_unknown_session_is_404_and_the_bearer_is_checked(
    app: FastAPI, lan: TestClient, pair_device: Any
) -> None:
    headers = _bearer(pair_device)

    assert lan.get("/api/sessions/20260101-000000/health", headers=headers).status_code == 404
    assert lan.get("/api/sessions/20260101-000000/health").status_code == 401
    assert lan.get("/api/sessions/..bad/health", headers=headers).status_code in (404, 422)


def test_a_failing_push_remote_shows_in_the_route(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    pair_body: Any,
) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "missing.git")],
        cwd=tmp_vault.path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    sync = GitSync(tmp_vault, VaultGitSettings())
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        sync=sync,
    )
    local = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    lan = TestClient(app, base_url="http://192.168.1.20:8765", client=("192.168.1.30", 50000))
    code = local.post("/api/pair/codes").json()["code"]
    token = lan.post("/api/pair", json=pair_body(code)).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    session_id = _start(lan, headers)

    assert sync.push_now() is False

    body = lan.get(f"/api/sessions/{session_id}/health", headers=headers).json()
    assert body["ok"] is False
    assert body["push"]["count"] >= 1
    assert body["push"]["message"] == "no hay conexión con GitHub"
