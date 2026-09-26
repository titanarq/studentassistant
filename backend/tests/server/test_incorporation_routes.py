"""Incremental incorporation over the server (#326): `GET .../sources/status`, "prepárame el tema"
in batched mode on the workspace stream, and `NotesGenerator.incorporate` for the chat router."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from generate_topic import GenerateTopic, make_topic, valid_notes
from studentassistant.config import (
    EditorSettings,
    LlmSettings,
    ObserverSettings,
    ServerSettings,
    Settings,
)
from studentassistant.editor.contradictions import TOOL_NAME as CONTRADICTIONS_TOOL
from studentassistant.editor.incorporate import SourceSetAsideError
from studentassistant.editor.revise import EDIT_TOOL
from studentassistant.llm import FakeClaude
from studentassistant.server.app import create_app
from studentassistant.server.notes_routes import NotesGenerator
from studentassistant.server.pairing import PairingCodes
from studentassistant.vault import Vault, put_source, read_notes

LOCAL_BASE_URL = "http://localhost:8765"
PAGE = "[Apuntes, página {n}](../sources/notes/page-00{n}.jpg)"


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def make_app(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> AppFactory:
    def make(transport: FakeClaude, editor: EditorSettings | None = None) -> FastAPI:
        return create_app(
            static_dir=tmp_path / "no-web-build",
            server=ServerSettings(devices_path=devices_path),
            codes=codes,
            vault=tmp_vault,
            llm_transport=transport,
            llm_settings=Settings(
                observer=ObserverSettings(enabled=False),
                llm=LlmSettings(),
                editor=editor or EditorSettings(doubts_in_chat=False, incorporate_batch_size=1),
            ),
        )

    return make


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def client(make_app: AppFactory, fake: FakeClaude) -> Iterator[TestClient]:
    with TestClient(make_app(fake), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        yield client


def _page(fake: FakeClaude, n: int) -> FakeClaude:
    return fake.reply_tool(
        EDIT_TOOL,
        {
            "summary": f"Incorporo la página {n}",
            "ops": [
                {
                    "op": "add_section",
                    "after": "" if n == 1 else f"pagina-{n - 1}",
                    "level": 2,
                    "title": f"Página {n}",
                    "anchor": f"pagina-{n}",
                    "text": f"Lo de la página {n}.[^p{n}]",
                }
            ],
            "footnotes": [{"label": f"p{n}", "definition": PAGE.format(n=n)}],
        },
        text=f"Incorporo la página {n}.",
    )


def test_sources_status_route(client: TestClient, topic: GenerateTopic) -> None:
    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "notes",
        "page.jpg",
        b"\xff\xd8 three",
        {"triage": {"status": "set_aside", "reasons": ["blank"]}},
    )

    response = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/sources/status")

    assert response.status_code == 200, response.text
    rows = response.json()["sources"]
    assert [(r["source_id"], r["state"]) for r in rows] == [
        ("sources/notes/page-001.jpg", "pendiente"),
        ("sources/notes/page-002.jpg", "pendiente"),
        ("sources/notes/page-003.jpg", "apartada"),
    ]
    assert rows[2]["reason"] == "página en blanco" and rows[2]["number"] == 3
    unknown = client.get(f"/api/subjects/{topic.subject}/topics/no-existe/sources/status")
    assert unknown.status_code == 404


def test_prepare_notes_runs_in_batches_on_the_workspace_stream(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    _page(_page(fake, 1), 2).reply_tool(CONTRADICTIONS_TOOL, {"contradictions": []})
    hub = client.app.state.workspace  # type: ignore[attr-defined]
    subscription = hub.subscribe(topic.subject, topic.topic)

    response = client.post(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is False and body["version"] == 1
    assert body["tag"] == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert len(fake.requests) == 3  # two incorporations, then the contradictions search
    first = str(fake.requests[0].messages)
    assert "page-001.jpg" in first and "page-002.jpg`:" not in first
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None and "{#pagina-2}" in notes

    events = [(e.event, dict(e.data)) for e in subscription.drain()]
    names = [name for name, _ in events if name != "reply.delta"]
    assert names == [
        "turn.started",
        "turn.result",
        "notes.changed",
        "incorporation.progress",
        "turn.started",
        "turn.result",
        "notes.changed",
        "incorporation.progress",
        "notes.changed",
    ]
    started = [data for name, data in events if name == "turn.started"]
    assert all(data["kind"] == "incorporate" for data in started)
    progress = [data for name, data in events if name == "incorporation.progress"]
    assert progress[-1] == {
        "done": 2,
        "total": 2,
        "source_ids": ["sources/notes/page-002.jpg"],
    }
    assert events[-1][1]["origin"] == "generation"
    status = client.get(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generation")
    assert status.json()["status"] == "done"


def test_single_mode_keeps_the_whole_topic_call(
    make_app: AppFactory, fake: FakeClaude, topic: GenerateTopic
) -> None:
    fake.reply_text(valid_notes(topic.session)).reply_tool(
        CONTRADICTIONS_TOOL, {"contradictions": []}
    )
    app = make_app(fake, EditorSettings(doubts_in_chat=False, prepare_mode="single"))
    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as local:
        response = local.post(f"/api/subjects/{topic.subject}/topics/{topic.topic}/notes/generate")

    assert response.status_code == 200, response.text
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    assert not fake.requests[0].tools or fake.requests[0].tools[0]["name"] != EDIT_TOOL


def test_the_generator_incorporates_a_few_sources_for_the_chat_router(
    client: TestClient, fake: FakeClaude, topic: GenerateTopic
) -> None:
    _page(fake, 1)
    generator: NotesGenerator = client.app.state.notes  # type: ignore[attr-defined]
    sessions = client.app.state.sessions  # type: ignore[attr-defined]
    replies: list[dict[str, Any]] = []

    async def on_reply(kind: str, data: dict[str, Any]) -> None:
        replies.append(data)

    async def run() -> Any:
        return await generator.incorporate(
            sessions,
            topic.subject,
            topic.topic,
            ["sources/notes/page-001.jpg"],
            on_reply=on_reply,
            turn_id="turn-x",
        )

    result = client.portal.call(run)  # type: ignore[union-attr]

    assert result.applied and result.turn_id == "turn-x" and replies
    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "notes",
        "page.jpg",
        b"\xff\xd8 three",
        {"triage": {"status": "set_aside", "reasons": ["blurry"]}},
    )

    async def refused() -> Any:
        return await generator.incorporate(
            sessions, topic.subject, topic.topic, ["sources/notes/page-003.jpg"]
        )

    with pytest.raises(SourceSetAsideError, match="La página 3 está apartada"):
        client.portal.call(refused)  # type: ignore[union-attr]
