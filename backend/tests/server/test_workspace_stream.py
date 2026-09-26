"""The workspace hub (`server/workspace.py`) and `GET .../workspace/stream` (SSE)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.editor.notes_format import notes_revision
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.workspace import (
    REPLY_DELTA,
    TurnBroadcast,
    WorkspaceClosedError,
    WorkspaceHub,
)
from studentassistant.server.workspace_routes import workspace_events
from studentassistant.vault import Vault

WAIT = 5.0  # seconds; every read of a stream is bounded, so a broken stream fails, never hangs


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


def _route(topic: ReviseTopic) -> str:
    return f"/api/subjects/{topic.subject}/topics/{topic.topic}/workspace/stream"


def _parse(chunk: bytes) -> tuple[str, Any]:
    text = chunk.decode()
    assert text.endswith("\n\n")
    if text.startswith(":"):
        return "comment", text[1:].strip()
    fields = dict(line.split(": ", 1) for line in text.strip("\n").splitlines())
    return fields["event"], json.loads(fields["data"])


async def _next(stream: AsyncIterator[bytes]) -> tuple[str, Any]:
    return _parse(await asyncio.wait_for(anext(stream), WAIT))


# -- the hub ---------------------------------------------------------------------------------------


def test_events_reach_only_their_topic() -> None:
    hub = WorkspaceHub()
    mine = hub.subscribe("s", "t")
    other = hub.subscribe("s", "u")
    hub.publish("s", "t", "notes.changed", {"revision": "r"})
    assert [(e.event, dict(e.data)) for e in mine.drain()] == [("notes.changed", {"revision": "r"})]
    assert other.drain() == []
    mine.close()
    other.close()
    assert hub.subscribers("s", "t") == 0


def test_a_full_queue_drops_reply_deltas_first() -> None:
    hub = WorkspaceHub(queue_size=3)
    subscription = hub.subscribe("s", "t")
    hub.publish("s", "t", "turn.started", {})
    hub.publish("s", "t", REPLY_DELTA, {"text": "a"})
    hub.publish("s", "t", REPLY_DELTA, {"text": "b"})
    hub.publish("s", "t", "turn.result", {})
    assert [e.event for e in subscription.drain()] == ["turn.started", REPLY_DELTA, "turn.result"]
    assert subscription.dropped == 1


@pytest.mark.anyio
async def test_a_turn_broadcast_publishes_its_sequence() -> None:
    hub = WorkspaceHub()
    subscription = hub.subscribe("s", "t")
    turn = TurnBroadcast(hub, "s", "t", origin="voice", request_id="req-3")
    turn.started()
    await turn.reply("reply.delta", {"text": "Hola", "attempt": 1})
    turn.error(502, "Claude no ha respondido.")
    events = subscription.drain()
    assert [e.event for e in events] == ["turn.started", "reply.delta", "turn.error"]
    assert events[1].data == {"text": "Hola", "attempt": 1, "turn_id": turn.turn_id}
    assert events[2].data == {
        "turn_id": turn.turn_id,
        "request_id": "req-3",
        "status": 502,
        "detail": "Claude no ha respondido.",
    }


@pytest.mark.anyio
async def test_a_closed_subscription_ends_its_reader() -> None:
    hub = WorkspaceHub()
    subscription = hub.subscribe("s", "t")
    reader = asyncio.create_task(subscription.get())
    await asyncio.sleep(0)
    hub.close()
    with pytest.raises(WorkspaceClosedError):
        await asyncio.wait_for(reader, WAIT)


# -- the stream ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_stream_sends_events_and_heartbeats_until_the_hub_closes() -> None:
    hub = WorkspaceHub()
    stream = workspace_events(hub, "s", "t", keepalive=0.05)
    assert await _next(stream) == ("comment", "connected")
    assert await _next(stream) == ("comment", "keep-alive")
    hub.publish("s", "t", "notes.changed", {"revision": "abc", "origin": "user", "summary": None})
    hub.publish("s", "u", "notes.changed", {"revision": "other"})
    event = await _next(stream)
    while event[0] == "comment":
        event = await _next(stream)
    assert event == ("notes.changed", {"revision": "abc", "origin": "user", "summary": None})
    hub.close()
    with pytest.raises(StopAsyncIteration):
        while True:
            await asyncio.wait_for(anext(stream), WAIT)
    assert hub.subscribers("s", "t") == 0


def _app(devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault) -> Any:
    return create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(devices_path=devices_path),
        codes=codes,
        vault=tmp_vault,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )


@pytest.mark.anyio
async def test_the_route_streams_the_topics_events(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, topic: ReviseTopic
) -> None:
    app = _app(devices_path, codes, tmp_path, topic.vault)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": _route(topic),
        "raw_path": _route(topic).encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"localhost:8765")],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8765),
    }
    sent: list[dict[str, Any]] = []
    gone = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    def body() -> bytes:
        return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    async def until(check: Any) -> None:
        async with asyncio.timeout(WAIT):
            while not check():
                await asyncio.sleep(0.01)

    served = asyncio.create_task(app(scope, receive, send))
    await until(lambda: b": connected" in body())
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"cache-control"] == b"no-cache"

    # The student's save, with no session running, reaches the stream.
    app.state.workspace.notes_changed(
        topic.subject, topic.topic, revision=notes_revision(topic.notes), origin="user", summary="x"
    )
    await until(lambda: b"event: notes.changed" in body())
    assert '"origin": "user"' in body().decode()

    gone.set()
    await asyncio.wait_for(served, WAIT)
    assert app.state.workspace.subscribers(topic.subject, topic.topic) == 0


def test_an_unknown_topic_is_404(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault
) -> None:
    app = _app(devices_path, codes, tmp_path, tmp_vault)
    with TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/subjects/nada/topics/nada/workspace/stream")
    assert response.status_code == 404
    assert response.json()["detail"] == "No existe ese tema en la bóveda."


def test_the_stream_needs_a_token_off_the_pc(
    devices_path: Path, codes: PairingCodes, tmp_path: Path, topic: ReviseTopic
) -> None:
    app = _app(devices_path, codes, tmp_path, topic.vault)
    with TestClient(
        app, base_url="http://192.168.1.20:8765", client=("192.168.1.30", 50000)
    ) as client:
        response = client.get(_route(topic))
    assert response.status_code == 401
