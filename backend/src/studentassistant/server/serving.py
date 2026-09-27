"""Running the app under uvicorn with a prompt shutdown on SIGTERM (#466).

Uvicorn's own shutdown closes the listening sockets, asks every connection to close (a WebSocket
gets close code 1012 and its handler returns), then waits for every running request task to finish
-- by default for as long as that takes -- and only then runs the ASGI lifespan shutdown (our
`_lifespan` `finally`: the bounded consumer stops and `SessionService.shutdown()`, the final vault
commit and push). A Server-Sent Events stream (the workspace chat's, the live view's) never
finishes on its own: it waits for the next event, and what would end it (`WorkspaceHub.close()`,
closing the bus) only runs in that lifespan shutdown, after uvicorn stopped waiting. So an open
review-UI tab held the process until systemd SIGKILLed it, skipping the final commit and push.

Two pieces fix it:

- `AppServer` is a `uvicorn.Server` whose `shutdown` first sets the app's `ShutdownSignal`
  (`app.state.shutdown`); every open-ended stream is wrapped in `until_shutdown`, so it ends (its
  generator closed, its subscription released) as soon as the signal is set, and uvicorn's wait
  for the request tasks returns at once.
- `serve_app` passes `[server] graceful_shutdown_seconds` as uvicorn's `timeout_graceful_shutdown`:
  a request still running after that bound (a tutor turn still streaming, a slow upload) is
  cancelled, and the lifespan shutdown -- the final commit and push -- still runs after it.
"""

from __future__ import annotations

import asyncio
import functools
import socket
from collections.abc import AsyncIterable, AsyncIterator

import uvicorn
from fastapi import FastAPI

from studentassistant.config import ServerSettings


class ShutdownSignal:
    """Set once when the server starts shutting down; `wait()` returns from then on.

    Not an `asyncio.Event`, which binds to the first event loop that waits on it: an app outlives
    the loop of a `TestClient` block, so each waiter's future is made in the loop that awaits it.
    """

    def __init__(self) -> None:
        self._set = False
        self._waiters: set[asyncio.Future[None]] = set()

    @property
    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        if self._set:
            return
        self._set = True
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.get_loop().call_soon_threadsafe(_resolve, waiter)

    async def wait(self) -> None:
        if self._set:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await waiter
        finally:
            self._waiters.discard(waiter)


def _resolve(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def begin_shutdown(app: FastAPI) -> None:
    """Tell the app's open-ended streams to end (idempotent)."""
    signal: ShutdownSignal = app.state.shutdown
    signal.set()


async def until_shutdown(
    stream: AsyncIterable[bytes], signal: ShutdownSignal
) -> AsyncIterator[bytes]:
    """`stream`'s chunks until it ends or `signal` is set; then the stream is closed and this ends.

    The step in progress when the signal comes (or when the reader goes away) is cancelled, so a
    generator parked on its queue runs its `finally` (releasing its subscription) right away. That
    step is never awaited here: under a cancelled anyio scope (a client disconnect) any await
    would be cancelled again, leaving the generator running while it was closed.
    """
    iterator = aiter(stream)
    stop = asyncio.ensure_future(signal.wait())
    step: asyncio.Future[bytes] | None = None
    try:
        while not signal.is_set:
            step = asyncio.ensure_future(anext(iterator))
            await asyncio.wait({step, stop}, return_when=asyncio.FIRST_COMPLETED)
            if not step.done():
                return
            try:
                chunk = step.result()
            except StopAsyncIteration:
                return
            step = None
            yield chunk
    finally:
        stop.cancel()
        if step is not None and not step.done():
            step.cancel()
            step.add_done_callback(functools.partial(_after_cancelled_step, iterator))
        else:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def _after_cancelled_step(iterator: AsyncIterator[bytes], step: asyncio.Future[bytes]) -> None:
    """Retrieve the cancelled step's outcome; close the stream if it swallowed the cancel."""
    if step.cancelled() or step.exception() is not None:
        return  # the cancel (or an error) ended the generator: its `finally` has run
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        asyncio.ensure_future(aclose())


class AppServer(uvicorn.Server):
    """A `uvicorn.Server` that sets the app's `ShutdownSignal` before its own shutdown."""

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        app = self.config.app
        if isinstance(app, FastAPI):
            begin_shutdown(app)
        await super().shutdown(sockets)


def server_config(app: FastAPI, server: ServerSettings) -> uvicorn.Config:
    # No proxy sits in front: never let `X-Forwarded-For` rewrite the client address the LAN
    # guard and the loopback trust see (uvicorn trusts it from loopback by default).
    return uvicorn.Config(
        app,
        host=server.host,
        port=server.port,
        proxy_headers=False,
        timeout_graceful_shutdown=server.graceful_shutdown_seconds,
    )


def serve_app(app: FastAPI, server: ServerSettings) -> None:
    """Serve `app` on `server.host:server.port` until SIGINT/SIGTERM (see the module doc)."""
    AppServer(server_config(app, server)).run()


__all__ = [
    "AppServer",
    "ShutdownSignal",
    "begin_shutdown",
    "serve_app",
    "server_config",
    "until_shutdown",
]
