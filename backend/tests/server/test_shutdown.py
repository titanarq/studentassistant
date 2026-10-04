"""A prompt shutdown (#466): open live streams and capture sockets never hold the server.

The end-to-end test runs the real `AppServer` (uvicorn) on a loopback port, so what it checks is
uvicorn's own shutdown sequence: the open workspace stream, live stream and capture WebSocket end
at once, and the app's lifespan shutdown (the final vault commit) still runs after them. Its
`timeout_graceful_shutdown` is far longer than the bound it asserts, so passing does not lean on
uvicorn cancelling the requests. Every wait is bounded, so a regression fails, never hangs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2 as httpx
import pytest
import websockets

from studentassistant.config import ObserverSettings, ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.serving import (
    AppServer,
    ShutdownSignal,
    server_config,
    until_shutdown,
)
from studentassistant.server.sessions import SessionService
from studentassistant.vault import Vault, create_subject, create_topic

WAIT = 10.0  # seconds; the bound on every step of the test
PROMPT = 3.0  # seconds; shutdown with every connection open must finish within this
GRACE = 60.0  # uvicorn's own bound; far above PROMPT, so it is not what makes the test pass


# -- until_shutdown --------------------------------------------------------------------------------


async def _forever(closed: list[str]) -> AsyncIterator[bytes]:
    try:
        yield b"first"
        await asyncio.Event().wait()
        yield b"never"
    finally:
        closed.append("closed")


def test_until_shutdown_ends_a_parked_stream_and_closes_it() -> None:
    async def scenario() -> tuple[list[bytes], list[str]]:
        signal = ShutdownSignal()
        closed: list[str] = []
        chunks: list[bytes] = []

        async def read() -> None:
            async for chunk in until_shutdown(_forever(closed), signal):
                chunks.append(chunk)

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.05)
        signal.set()
        await asyncio.wait_for(reader, WAIT)
        return chunks, closed

    assert asyncio.run(scenario()) == ([b"first"], ["closed"])


def test_until_shutdown_passes_a_finite_stream_through() -> None:
    async def finite() -> AsyncIterator[bytes]:
        for chunk in (b"a", b"b"):
            yield chunk

    async def scenario() -> list[bytes]:
        return [chunk async for chunk in until_shutdown(finite(), ShutdownSignal())]

    assert asyncio.run(asyncio.wait_for(scenario(), WAIT)) == [b"a", b"b"]


def test_a_set_signal_ends_a_stream_before_its_first_chunk() -> None:
    signal = ShutdownSignal()
    signal.set()

    async def scenario() -> tuple[list[bytes], list[str]]:
        closed: list[str] = []
        chunks = [chunk async for chunk in until_shutdown(_forever(closed), signal)]
        return chunks, closed

    chunks, _ = asyncio.run(asyncio.wait_for(scenario(), WAIT))
    assert chunks == []


def test_the_signal_serves_waiters_in_different_event_loops() -> None:
    signal = ShutdownSignal()

    async def wait_briefly() -> bool:
        try:
            await asyncio.wait_for(signal.wait(), 0.05)
        except TimeoutError:
            return False
        return True

    assert asyncio.run(wait_briefly()) is False
    signal.set()
    assert asyncio.run(wait_briefly()) is True


# -- the real server -------------------------------------------------------------------------------


def test_shutdown_with_open_streams_and_socket_is_prompt_and_still_commits(
    tmp_path: Path, tmp_vault: Vault, student_user_id: str
) -> None:
    user_vault = tmp_vault.for_user(student_user_id)
    subject = create_subject(user_vault, "Biología")
    create_topic(user_vault, subject.slug, "Fotosíntesis")
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=ServerSettings(
            host="127.0.0.1",
            port=0,
            devices_path=tmp_path / "devices.json",
            graceful_shutdown_seconds=GRACE,
        ),
        vault=tmp_vault,
        llm_settings=Settings(observer=ObserverSettings(enabled=False)),
    )
    sessions: SessionService = app.state.sessions
    shutdowns: list[str] = []
    real_shutdown = sessions.shutdown

    async def recorded_shutdown() -> None:
        await real_shutdown()
        shutdowns.append("done")

    sessions.shutdown = recorded_shutdown  # type: ignore[method-assign]
    server = AppServer(server_config(app, app.state.server))

    async def scenario() -> float:
        serving = asyncio.create_task(server.serve())
        while not server.started:
            assert not serving.done(), "the server did not start"
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        opened: list[str] = []
        ended: list[str] = []

        async with httpx.AsyncClient(base_url=base, timeout=WAIT) as client:
            assert (
                await client.post("/api/subjects", json={"name": "Biología"})
            ).status_code == 201
            created = await client.post(
                "/api/subjects/biologia/topics", json={"name": "Fotosíntesis"}
            )
            assert created.status_code == 201
            started = await client.post(
                "/api/sessions",
                json={
                    "subject_id": "biologia",
                    "topic_id": "fotosintesis",
                    "client_time_ms": int(time.time() * 1000),
                },
            )
            assert started.status_code == 201
            session_id = started.json()["session_id"]

            async def stream(name: str, path: str) -> None:
                async with client.stream("GET", path, timeout=None) as response:
                    assert response.status_code == 200
                    async for _ in response.aiter_bytes():
                        if name not in opened:
                            opened.append(name)
                ended.append(name)

            async def socket() -> None:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws/sessions/{session_id}"
                ) as ws:
                    hello = {
                        "type": "hello",
                        "protocol_version": "1.7",
                        "capabilities": {"stt": "client", "stt_provider": "web-speech"},
                        "client_time_ms": int(time.time() * 1000),
                    }
                    await ws.send(json.dumps(hello))
                    assert json.loads(await ws.recv())["type"] == "hello.ack"
                    opened.append("socket")
                    with pytest.raises(websockets.ConnectionClosed):
                        while True:
                            await ws.recv()
                ended.append("socket")

            clients = [
                asyncio.create_task(
                    stream(
                        "workspace", "/api/subjects/biologia/topics/fotosintesis/workspace/stream"
                    )
                ),
                asyncio.create_task(stream("live", "/api/live")),
                asyncio.create_task(socket()),
            ]
            deadline = time.monotonic() + WAIT
            while len(opened) < 3:
                assert time.monotonic() < deadline, f"only {opened} opened"
                for task in clients:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.02)

            began = time.monotonic()
            server.should_exit = True  # what uvicorn's SIGTERM handler does
            await asyncio.wait_for(serving, WAIT)
            took = time.monotonic() - began
            await asyncio.wait_for(asyncio.gather(*clients, return_exceptions=True), WAIT)
        assert sorted(ended) == ["live", "socket", "workspace"]
        return took

    took = asyncio.run(scenario())

    assert took < PROMPT
    assert shutdowns == ["done"]
