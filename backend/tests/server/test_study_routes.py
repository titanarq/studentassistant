"""Switching a topic to Estudiar (#335): `POST/GET .../study` and the chat intent `study`.

Every test runs the real app (`FakeClaude`, `tmp_vault`, the FastAPI test client). The observer
loop stays off, so the only scripted replies are the typed messages' classifications. Every wait
is bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import (
    EditorSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
    SourcesSettings,
)
from studentassistant.generators.run import ArtifactMeta, meta_name, notes_sha256
from studentassistant.llm import FakeClaude
from studentassistant.observer import ASSISTANT_REQUEST_KIND
from studentassistant.observer.requests import TOOL_NAME
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import WorkspaceEvent, WorkspaceSubscription
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sessions,
    read_notes,
    write_generated,
    write_notes,
)

LOCAL_BASE_URL = "http://localhost:8765"
WAIT_SECONDS = 10.0
AppFactory = Callable[..., FastAPI]
Topic = GenerateTopic | ReviseTopic


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude | None) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            sources=SourcesSettings(transcription_enabled=False),
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False, request_detection="observer"),
                editor=EditorSettings(doubts_in_chat=False),
            ),
        )

    return make


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with _client(make_app(fake)) as client:
        yield client


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _base(topic: Topic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}"


def _start(client: TestClient, topic: Topic) -> str:
    started = client.post(
        "/api/sessions",
        json={"subject_id": topic.subject, "topic_id": topic.topic, "client_time_ms": 1_000},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["session_id"])


def _ended(topic: Topic, session_id: str) -> bool:
    meta = {m.id: m for m in list_sessions(topic.vault, topic.subject, topic.topic)}
    return meta[session_id].ended_at is not None


def _subscribe(client: TestClient, topic: Topic) -> WorkspaceSubscription:
    app: Any = client.app
    return app.state.workspace.subscribe(topic.subject, topic.topic)  # type: ignore[no-any-return]


def _settle(client: TestClient) -> None:
    app: Any = client.app
    consumer: AssistantRequestConsumer = app.state.assistant_requests
    client.portal.call(consumer.wait_idle, WAIT_SECONDS)  # type: ignore[union-attr]


def _names(events: list[WorkspaceEvent]) -> list[str]:
    return [event.event for event in events if event.event != "reply.delta"]


def _material(topic: Topic, kind: str, *, notes_version: int | None = 1) -> None:
    """A generated `kind` built from the current notes (a manifest, as `run_generator` writes)."""
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    meta = ArtifactMeta.model_validate(
        {
            "kind": kind,
            "generator_version": 99,
            "built_at": datetime(2026, 9, 27, tzinfo=UTC),
            "notes": {"sha256": notes_sha256(notes), "version": notes_version},
            "files": [f"{kind}.md"],
        }
    )
    write_generated(topic.vault, topic.subject, topic.topic, f"{kind}.md", "# Material\n")
    write_generated(
        topic.vault,
        topic.subject,
        topic.topic,
        meta_name(kind),
        yaml.safe_dump(meta.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
    )


def _options(body: dict[str, Any]) -> dict[str, tuple[str, str, str | None]]:
    return {o["key"]: (o["kind"], o["state"], o.get("stale_reason")) for o in body["options"]}


# -- POST .../study --------------------------------------------------------------------------------


def test_switching_ends_the_capture_without_preparing_notes_and_labels_the_version(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)

    response = client.post(f"{_base(topic)}/study")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ended_session"] == session_id and body["created_tag"] is True
    assert body["study_version"]["version"] == 1
    assert body["study_version"]["tag"] == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert body["study_current"] is True
    assert {o["key"]: o["state"] for o in body["options"]} == {
        "esquema": "sin_generar",
        "ejercicios": "sin_generar",
        "examen": "sin_generar",
        "quiz": "sin_generar",
        "tarjetas": "sin_generar",
    }
    app: Any = client.app
    assert app.state.sessions.active is None and _ended(topic, session_id)
    # No "prepárame el tema": no Claude call, no generation.
    assert fake.requests == []
    assert app.state.notes.status(topic.subject, topic.topic).status == "idle"
    events = subscription.drain()
    [marked] = [e.data for e in events if e.event == "study.marked"]
    assert marked == {"version": 1, "tag": f"{topic.subject}/{topic.topic}/apuntes-v1"}
    assert app.state.notes.holder(topic.subject, topic.topic) is None  # lock released

    # Again, unchanged: the same version, nothing new tagged, no session to end.
    again = client.post(f"{_base(topic)}/study").json()
    assert again["study_version"] == body["study_version"]
    assert again["created_tag"] is False and again["ended_session"] is None
    tags = GitSync(topic.vault).list_notes_tags(topic.subject, topic.topic)
    assert [t.version for t in tags] == [1]

    listing = client.get(f"{_base(topic)}/notes/versions").json()
    assert listing["versions"][0]["study"] is True
    assert listing["study_version"]["version"] == 1 and listing["study_current"] is True


def test_switching_works_without_claude(make_app: AppFactory, topic: ReviseTopic) -> None:
    with _client(make_app(None)) as client:
        response = client.post(f"{_base(topic)}/study")
        assert response.status_code == 200, response.text
        assert response.json()["study_version"]["version"] == 1
        assert client.get(f"{_base(topic)}/study").json()["study_current"] is True


def test_switching_is_refused_while_the_notes_are_busy(
    client: TestClient, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    app: Any = client.app
    assert app.state.notes.claim(topic.subject, topic.topic, "editor")

    response = client.post(f"{_base(topic)}/study")

    assert response.status_code == 409
    assert "trabajando" in response.json()["detail"]
    assert app.state.sessions.active is not None and not _ended(topic, session_id)
    app.state.notes.release(topic.subject, topic.topic)


def test_switching_without_notes_is_refused_and_ends_nothing(
    client: TestClient, tmp_vault: Vault
) -> None:
    topic = make_topic(tmp_vault)
    session_id = _start(client, topic)

    response = client.post(f"{_base(topic)}/study")

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Todavía no hay apuntes")
    assert not _ended(topic, session_id)


def test_an_unknown_topic_is_404(client: TestClient, topic: ReviseTopic) -> None:
    assert client.post(f"/api/subjects/{topic.subject}/topics/nope/study").status_code == 404
    assert client.get(f"/api/subjects/{topic.subject}/topics/nope/study").status_code == 404


# -- GET .../study ---------------------------------------------------------------------------------


def test_the_state_reports_each_option_and_goes_stale_after_an_edit(
    client: TestClient, topic: ReviseTopic
) -> None:
    before = client.get(f"{_base(topic)}/study").json()
    assert before["study_version"] is None and before["study_current"] is False

    client.post(f"{_base(topic)}/study")
    _material(topic, "quiz")
    _material(topic, "examen")
    _material(topic, "flashcards")

    state = client.get(f"{_base(topic)}/study").json()
    assert state["study_current"] is True
    assert _options(state) == {
        "esquema": ("esquema", "sin_generar", None),
        "ejercicios": ("examen", "listo", None),
        "examen": ("examen", "listo", None),
        "quiz": ("quiz", "listo", None),
        "tarjetas": ("flashcards", "listo", None),
    }
    assert {o["key"]: o.get("notes_version") for o in state["options"]}["quiz"] == 1

    # An edit after labelling: still labelled, but not current, and the materials are stale.
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    write_notes(topic.vault, topic.subject, topic.topic, notes.replace("límite", "límite (lím)"))
    after = client.get(f"{_base(topic)}/study").json()
    assert after["study_version"] == state["study_version"]
    assert after["study_current"] is False
    options = _options(after)
    assert options["quiz"] == (
        "quiz",
        "desactualizado",
        "Los apuntes han cambiado desde que se generó.",
    )
    assert options["tarjetas"][1] == "desactualizado"
    assert options["esquema"][1] == "sin_generar"


# -- the chat intent -------------------------------------------------------------------------------


def _classify_study(fake: FakeClaude) -> None:
    fake.reply_tool(
        TOOL_NAME,
        {"requests": [{"kind": "study", "summary": "Pasar a estudiar", "segment_ids": ["m1"]}]},
    )


def test_a_typed_study_message_ends_the_capture_and_offers_go_study(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    _classify_study(fake)

    response = client.post(
        f"{_base(topic)}/workspace/messages", json={"text": "ya está, quiero estudiar"}
    )
    assert response.status_code == 202, response.text
    [request] = response.json()["requests"]
    assert request["kind"] == "study"
    _settle(client)

    app: Any = client.app
    assert app.state.sessions.active is None and _ended(topic, session_id)
    assert len(fake.requests) == 1  # only the classification: no notes generation
    events = subscription.drain()
    assert _names(events) == ["request.detected", "turn.started", "study.marked", "turn.result"]
    started = events[1].data
    assert started["kind"] == "study" and started["origin"] == "typed"
    reply = "He cerrado la captura y marcado los apuntes v1 como versión de estudio."
    assert "".join(e.data["text"] for e in events if e.event == "reply.delta") == reply
    result = next(e.data for e in events if e.event == "turn.result")
    assert result["kind"] == "study" and result["reply"] == reply
    assert result["action"] == {
        "kind": "go_study",
        "path": f"/subjects/{topic.subject}/topics/{topic.topic}/study",
    }
    assert result["study"]["study_version"]["version"] == 1
    assert result["message"] == "ya está, quiero estudiar"


def test_a_spoken_study_request_is_dispatched_to_the_same_service(
    client: TestClient, fake: FakeClaude, topic: ReviseTopic
) -> None:
    session_id = _start(client, topic)
    subscription = _subscribe(client, topic)
    bus = client.app.state.bus  # type: ignore[attr-defined]
    payload = {
        "request_id": "req-1",
        "kind": "study",
        "summary": "Pasar a estudiar",
        "text": "vale, pasa a estudiar",
        "segment_ids": ["seg-1"],
        "t_start_ms": 1_000,
        "t_end_ms": 1_800,
        "detector": "observer",
    }

    async def publish() -> None:
        await bus.publish(session_id, ASSISTANT_REQUEST_KIND, "observer", payload)

    client.portal.call(publish)  # type: ignore[union-attr]
    _settle(client)

    assert _ended(topic, session_id)
    events = subscription.drain()
    result = next(e.data for e in events if e.event == "turn.result")
    assert result["origin"] == "voice" and result["request"]["request_id"] == "req-1"
    assert result["action"]["kind"] == "go_study"
    assert fake.requests == []


def test_a_study_request_without_notes_is_a_spanish_turn_error(
    client: TestClient, fake: FakeClaude, tmp_vault: Vault
) -> None:
    topic = make_topic(tmp_vault)
    subscription = _subscribe(client, topic)
    _classify_study(fake)

    client.post(f"{_base(topic)}/workspace/messages", json={"text": "vamos a estudiar esto"})
    _settle(client)

    events = subscription.drain()
    [error] = [e.data for e in events if e.event == "turn.error"]
    assert error["status"] == 409 and error["detail"].startswith("Todavía no hay apuntes")
    assert not [e for e in events if e.event == "study.marked"]
