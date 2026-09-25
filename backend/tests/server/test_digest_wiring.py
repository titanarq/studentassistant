"""The topic digest in the app: regenerated at every session end, served to the web, and handed to
the observer and the editor as their digest reader."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import make_topic, valid_notes
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.llm import FakeClaude
from studentassistant.observer import DigestOnEnd, topic_digest
from studentassistant.server.app import create_app
from studentassistant.server.bus import SessionBus
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import SessionService
from studentassistant.vault import Vault, read_topic_digest, write_topic_digest


@pytest.fixture
def app(server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=tmp_vault
    )


def _run_session(local: TestClient, *ops: dict[str, object]) -> str:
    local.post("/api/subjects", json={"name": "Física"})
    local.post("/api/subjects/fisica/topics", json={"name": "Cinemática"})
    started = local.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    app: FastAPI = local.app  # type: ignore[assignment]
    for op in ops:
        local.portal.call(  # type: ignore[union-attr]
            app.state.bus.publish, session_id, "observer.state_op", "observer", op
        )
    ended = local.post(
        f"/api/sessions/{session_id}/end", json={"client_time_ms": 2, "reason": "button"}
    )
    assert ended.status_code == 200, ended.text
    return session_id


def test_ending_a_session_writes_the_digest_the_routes_serve(
    local: TestClient, tmp_vault: Vault
) -> None:
    with local:
        empty = local.get("/api/subjects/fisica/topics/cinematica/digest")
        assert empty.status_code == 404  # no such topic yet

        _run_session(local, {"op": "add_section", "section_id": "sec-1", "title": "Velocidad"})

        stored = read_topic_digest(tmp_vault, "fisica", "cinematica")
        assert stored is not None and "- Velocidad" in stored and "(terminada," in stored
        body = local.get("/api/subjects/fisica/topics/cinematica/digest").json()
        assert body["text"] == stored
        assert body["excerpt"].startswith("1 sesión con contenido; la última, el ")
        summary = local.get("/api/subjects/fisica/topics/cinematica/summary").json()
        assert summary["digest_excerpt"] == body["excerpt"]


def test_a_topic_without_a_digest_answers_null(local: TestClient) -> None:
    with local:
        local.post("/api/subjects", json={"name": "Física"})
        local.post("/api/subjects/fisica/topics", json={"name": "Óptica"})
        body = local.get("/api/subjects/fisica/topics/optica/digest").json()
        assert body == {"subject_id": "fisica", "topic_id": "optica", "text": None, "excerpt": None}
        summary = local.get("/api/subjects/fisica/topics/optica/summary").json()
        assert summary["digest_excerpt"] is None


def test_the_end_hook_regenerates_the_ending_sessions_digest(tmp_vault: Vault) -> None:
    async def main() -> str | None:
        service = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")
        service.add_before_close(DigestOnEnd(service.bus.attached))
        subject = await service.create_subject("Física")
        topic = await service.create_topic(subject.subject_id, "Cinemática")
        started = await service.start(subject.subject_id, topic.topic_id, client_time_ms=1)
        await service.end(started.session_id, client_time_ms=5, reason="button")
        return read_topic_digest(tmp_vault, subject.subject_id, topic.topic_id)

    text = asyncio.run(asyncio.wait_for(main(), 10))
    assert text is not None and "# Resumen del tema: Cinemática" in text


def test_the_end_hook_skips_a_session_it_cannot_find() -> None:
    asyncio.run(asyncio.wait_for(DigestOnEnd(lambda _id: None)("20260924-180000"), 5))


def test_the_observer_reads_the_stored_digest(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        llm_transport=FakeClaude(),
        llm_settings=Settings(observer=ObserverSettings()),
    )
    assert app.state.observer.digest is topic_digest


def test_the_editor_gets_the_stored_digest(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    topic = make_topic(tmp_vault)
    write_topic_digest(tmp_vault, topic.subject, topic.topic, "# Resumen\n\nSesión 1: límites.\n")
    fake = FakeClaude()
    fake.reply_text(valid_notes(topic.session))
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        llm_transport=fake,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    with TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000)) as client:
        response = client.post(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate")

    assert response.status_code == 200, response.text
    sent = str(fake.requests[0].messages)
    assert "Resumen del tema (sesiones anteriores)" in sent and "Sesión 1: límites." in sent
