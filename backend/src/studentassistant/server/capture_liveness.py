"""Capture liveness: the backend ends a capture session no client is sending to (#425).

A capture session stays alive only while a capture client (the web `/capture` page, the
workspace's Captura tab, the Android app) is connected and sending. The human's rule: «La pestaña
de captura está funcionando mientras está visible y el móvil (o la webcam del portátil) está
conectado y enviando datos.»

The active session is *sending* while at least one of its capture WebSockets (`ws.py`) has said
`hello` and has not declared itself paused: each socket's latest `button` `pause` / `resume` sets
its own flag, and a closed socket stops counting. The WebSocket gateway reports those changes
here (`connected`, `set_paused`, `disconnected`).

A pause says why since protocol 1.7 (#454): `reason: "hidden"` -- or no reason, what older
clients and the Android app send when they go to the background -- means the client stopped
sending, and the idle clock runs; `reason: "student"` is the web workspace's Recursos tab, a page
still in front of the student who only put the capture aside, and that socket keeps the session
*held*: while it stays connected the idle clock does not run. The human's rule stands: a hidden
tab stops sending and auto-ends; a closed socket stops counting whatever it said.

The idle clock of the watched session (the one `SessionService` last attached, on `start` or
`resume`) starts when it stops sending and is reset as soon as a socket connects or resumes; a
session just started or resumed with no socket yet counts as not sending, so its grace period
starts at `session.started` / `session.resumed`. Once the clock reaches `[server]
capture_idle_end_seconds` (default 300 s, longer than the web's 2-minute reconnect window) the
watchdog ends the session through the normal `SessionService.end` path with `reason: "idle"` and
`idle_seconds` in the `session.ended` payload. It never generates anything (no `prepare_notes`).

A failed auto-end (the session was ended concurrently, the vault failed) is logged and never
raised; after an unexpected failure the grace period starts again. A socket still open when the
session auto-ended is closed as not active on its next message, like after an explicit end, with a
close reason ending in `(idle)` (`ended_idle`) so the client can say why.

The watchdog loop (`start` .. `stop`) runs with the app's lifespan and checks every `interval`
seconds; tests call `tick()` with an injected clock instead.

Whose session the watchdog is watching is not something it needs to know (#550): it watches the one
session `SessionService` last attached, a backend has room for one active session whatever student
it belongs to, and the `end` it calls names the session by its id -- the service looks the id up
across the users' folders itself and writes through the handle of the user the session was opened
for. The sockets it counts are that session's and nobody else's, because a handshake for a session
of another user is refused before it can register (`ws.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from studentassistant.server.sessions import SessionConflictError, SessionService

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SECONDS = 5.0
"""How often the watchdog loop checks the watched session (capped by the grace period)."""

DEFAULT_STOP_TIMEOUT_SECONDS = 15.0
"""How long `stop` waits for an auto-end in progress before cancelling it."""

IDLE_CLOSE_MARK = "(idle)"
"""What the close reason of a socket of an auto-ended session ends with."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class _Socket:
    session_id: str
    paused: bool = False
    # Paused with `reason: "student"` (#454): not sending, but the client is there.
    held: bool = False


class CaptureLiveness:
    """Tracks whether the active session has a capture client sending, and ends it when not.

    `grace_seconds` is `[server] capture_idle_end_seconds`; `clock` gives seconds (monotonic by
    default; tests pass a `FakeClock`); `wall_clock_ms` gives the epoch ms put in the
    `session.ended` payload's `client_time_ms`. It registers itself on `sessions` as an attached
    hook.
    """

    def __init__(
        self,
        sessions: SessionService,
        *,
        grace_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] = _now_ms,
        interval: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    ) -> None:
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self.sessions = sessions
        self.grace_seconds = grace_seconds
        self.clock = clock
        self._wall_clock_ms = wall_clock_ms
        self.interval = min(interval, grace_seconds)
        self._tokens = itertools.count(1)
        self._sockets: dict[int, _Socket] = {}
        self._watched: str | None = None
        self._idle_since: float | None = None
        self._idle_ended: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None
        sessions.add_on_attached(self._on_attached)

    # -- what the gateway reports --------------------------------------------------------------

    def connected(self, session_id: str) -> int:
        """A capture socket of `session_id` said hello; the token names it in later reports."""
        token = next(self._tokens)
        self._sockets[token] = _Socket(session_id)
        self._update(session_id)
        return token

    def set_paused(self, token: int, paused: bool, reason: str | None = None) -> None:
        """The socket's latest `button` was `pause` (True) or `resume` (False).

        A pause's `reason` `student` (#454) keeps the session held while the socket is connected;
        `hidden` or none lets the idle clock run. A new pause with another reason replaces it.
        """
        socket = self._sockets.get(token)
        held = paused and reason == "student"
        if socket is None or (socket.paused, socket.held) == (paused, held):
            return
        socket.paused = paused
        socket.held = held
        self._update(socket.session_id)

    def disconnected(self, token: int) -> None:
        """The socket closed; it no longer counts."""
        socket = self._sockets.pop(token, None)
        if socket is not None:
            self._update(socket.session_id)

    # -- state ---------------------------------------------------------------------------------

    def is_sending(self, session_id: str) -> bool:
        """Whether a socket of `session_id` is connected and not paused."""
        return any(s.session_id == session_id and not s.paused for s in self._sockets.values())

    def is_held(self, session_id: str) -> bool:
        """Whether a socket of `session_id` is connected and sending, or paused by the student
        in a client still in front of them (`reason: "student"`, #454): the idle clock stops."""
        return any(
            s.session_id == session_id and (not s.paused or s.held) for s in self._sockets.values()
        )

    def idle_seconds(self, session_id: str) -> float | None:
        """Seconds the watched `session_id` has not been sending; None if sending or unwatched."""
        if session_id != self._watched or self._idle_since is None:
            return None
        return max(0.0, self.clock() - self._idle_since)

    def ended_idle(self, session_id: str) -> bool:
        """Whether the watchdog ended (or is ending) `session_id` for being idle."""
        return session_id in self._idle_ended

    # -- the watchdog --------------------------------------------------------------------------

    async def tick(self) -> str | None:
        """Check the watched session once; the id of the session it auto-ended, else None."""
        session_id = self._watched
        if session_id is None:
            return None
        active = self.sessions.active
        if active is None or active.session_id != session_id:
            self._forget()  # ended or replaced some other way
            return None
        idle = self.idle_seconds(session_id)
        if idle is None or idle < self.grace_seconds:
            return None
        logger.info(
            "capture session %s: no capture client sending for %.0f s; ending it", session_id, idle
        )
        self._idle_ended.add(session_id)
        try:
            await self.sessions.end(
                session_id,
                client_time_ms=self._wall_clock_ms(),
                reason="idle",
                idle_seconds=idle,
            )
        except SessionConflictError as error:
            # Ended (or replaced) concurrently: nothing left to do.
            self._idle_ended.discard(session_id)
            logger.info("idle end of session %s skipped: %s", session_id, error)
        except Exception:
            self._idle_ended.discard(session_id)
            logger.exception("idle end of session %s failed; its grace period restarts", session_id)
            if self._watched == session_id and self._idle_since is not None:
                self._idle_since = self.clock()
            return None
        else:
            if self._watched == session_id:
                self._forget()
            return session_id
        if self._watched == session_id:
            self._forget()
        return None

    def start(self) -> None:
        """Start the watchdog loop on the running event loop (the app's lifespan)."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._stopping), name="capture-liveness")

    async def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Stop the loop; an auto-end in progress gets `timeout` seconds, then is cancelled."""
        task, self._task = self._task, None
        if self._stopping is not None:
            self._stopping.set()
        if task is None:
            return
        _done, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            logger.warning("capture liveness watchdog did not stop within %.1f s", timeout)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), self.interval)
            if stopping.is_set():
                return
            try:
                await self.tick()
            except Exception:
                logger.exception("capture liveness check failed")

    # -- internals -----------------------------------------------------------------------------

    def _on_attached(self, session_id: str) -> None:
        """`start` / `resume`: watch this session; its grace period starts now unless sending."""
        if session_id != self._watched:
            self._sockets = {t: s for t, s in self._sockets.items() if s.session_id == session_id}
        self._watched = session_id
        self._idle_since = None if self.is_held(session_id) else self.clock()

    def _update(self, session_id: str) -> None:
        """Start or reset the idle clock after a socket change of `session_id`."""
        if session_id != self._watched:
            return
        if self.is_held(session_id):
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = self.clock()

    def _forget(self) -> None:
        self._watched = None
        self._idle_since = None


__all__ = [
    "DEFAULT_CHECK_INTERVAL_SECONDS",
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "IDLE_CLOSE_MARK",
    "CaptureLiveness",
]
