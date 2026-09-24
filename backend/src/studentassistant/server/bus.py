"""The in-process session event bus: every session event, persisted or not, goes through here.

Feature modules (stt, sources, observer, editor, the WebSocket gateway) publish on one app-wide
`SessionBus` and subscribe to it; the lifecycle service (`sessions.py`) attaches a session's vault
handle when the session becomes active and detaches it when it ends.

Two kinds of message travel on it:

- **Persisted events** (`persist=True`, the default): the envelope of ADR-0003. Each one is
  appended to the session's `events.jsonl` through the vault's `Session` handle (ADR-0002: the bus
  never writes a file itself), which assigns the next `seq`; only then is it delivered, so a
  subscriber never sees an event the log does not hold. The disk write runs in a worker thread.
- **Notices** (`persist=False`): transient messages (a transcript partial, a pending count) that
  are delivered but never written; they carry no `seq`.

Publishes are serialised, so every subscriber sees events in `seq` order and notices in the order
they were published relative to them. Delivery never waits for a subscriber: each subscription has
a bounded queue, and when it is full the oldest *notice* in it is dropped to make room (a notice
arriving at a queue holding no notice is itself dropped). A persisted event is never dropped: when
the queue holds only persisted events it grows past its bound (counted in `overflowed` and
logged), because the subscriber must see every one of them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from studentassistant.vault import EVENT_SCHEMA_VERSION, Session
from studentassistant.vault.session_models import Origin

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 256
"""How many messages a subscription holds before it starts dropping notices."""


class BusError(Exception):
    """Something the bus refuses to do; the message says why."""


class SessionNotAttachedError(BusError, LookupError):
    """A publish named a session the bus has no open handle for (unknown, ended or not active)."""


@dataclass(frozen=True)
class BusEvent:
    """One message on the bus: a persisted event (with its `seq`) or a notice (`seq` is None).

    `t` is milliseconds since the session's `started_at`, as in `events.jsonl`. `payload` belongs
    to every subscriber at once: read it, never mutate it.
    """

    session_id: str
    subject_id: str
    topic_id: str
    kind: str
    origin: Origin
    t: int
    payload: Mapping[str, Any]
    seq: int | None = None
    schema_version: int = EVENT_SCHEMA_VERSION

    @property
    def persisted(self) -> bool:
        return self.seq is not None


class Subscription:
    """A subscriber's view of the bus: a bounded queue of the `BusEvent`s its filter lets through.

    Read it with `await get()` or `async for event in subscription`; iteration ends once the
    subscription is closed and its queue drained. Close it (`close()`, or leave its `with` /
    `async with` block) when done, so the bus stops filling it.
    """

    def __init__(
        self,
        bus: SessionBus,
        *,
        name: str,
        session_id: str | None,
        kinds: Collection[str] | None,
        maxsize: int,
    ) -> None:
        if maxsize < 1:
            raise ValueError("a subscription queue holds at least one message")
        self.name = name
        self.session_id = session_id
        self.kinds = None if kinds is None else frozenset(kinds)
        self.maxsize = maxsize
        self.dropped = 0
        """Notices dropped because the queue was full."""
        self.overflowed = 0
        """Persisted events queued past `maxsize` (never dropped)."""
        self._bus = bus
        self._queue: deque[BusEvent] = deque()
        self._waiter: asyncio.Future[None] | None = None
        self._closed = False
        self._lagging = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._queue)

    def wants(self, event: BusEvent) -> bool:
        if self.session_id is not None and event.session_id != self.session_id:
            return False
        return self.kinds is None or event.kind in self.kinds

    def offer(self, event: BusEvent) -> None:
        """Queue `event` without ever waiting; called by the bus on every publish it wants."""
        if self._closed:
            return
        if len(self._queue) >= self.maxsize and not self._make_room(event):
            return
        self._queue.append(event)
        self._wake()

    def _make_room(self, event: BusEvent) -> bool:
        """Free a slot for `event` in a full queue; False when `event` itself is dropped."""
        for index, queued in enumerate(self._queue):
            if not queued.persisted:
                del self._queue[index]
                self.dropped += 1
                return True
        if not event.persisted:
            self.dropped += 1
            return False
        self.overflowed += 1
        if not self._lagging:
            self._lagging = True
            logger.warning(
                "bus subscriber %r fell %d persisted events behind (queue bound %d); keeping them",
                self.name,
                len(self._queue) + 1,
                self.maxsize,
            )
        return True

    def get_nowait(self) -> BusEvent:
        """The next queued event; `asyncio.QueueEmpty` when there is none."""
        if not self._queue:
            raise asyncio.QueueEmpty
        return self._pop()

    async def get(self) -> BusEvent:
        """Wait for the next event; `BusError` once the subscription is closed and drained."""
        while not self._queue:
            if self._closed:
                raise BusError(f"subscription {self.name!r} is closed")
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._pop()

    def _pop(self) -> BusEvent:
        event = self._queue.popleft()
        if self._lagging and len(self._queue) < self.maxsize:
            self._lagging = False
        return event

    def __aiter__(self) -> AsyncIterator[BusEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[BusEvent]:
        while True:
            try:
                yield await self.get()
            except BusError:
                return

    def close(self) -> None:
        """Stop receiving; a reader waiting in `get()` gets what is queued, then the end."""
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self)
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class SessionBus:
    """App-wide async publish/subscribe of session events (one instance on `app.state.bus`).

    `on_append` is called (in the event loop, after the write) for every persisted event, so the
    owner can tell the vault's git sync that a file changed.
    """

    on_append: Callable[[], None] | None = None
    default_queue_size: int = DEFAULT_QUEUE_SIZE
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)
    _subscriptions: list[Subscription] = field(default_factory=list, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- sessions ------------------------------------------------------------------------------

    def attach(self, session: Session) -> None:
        """Accept publishes for `session` (its vault handle appends the persisted ones)."""
        self._sessions[session.id] = session

    def detach(self, session_id: str) -> None:
        """Refuse further publishes for the session; subscriptions stay open."""
        self._sessions.pop(session_id, None)

    def is_attached(self, session_id: str) -> bool:
        return session_id in self._sessions

    # -- publish -------------------------------------------------------------------------------

    async def publish(
        self,
        session_id: str,
        kind: str,
        origin: Origin,
        payload: Mapping[str, Any] | None = None,
        *,
        persist: bool = True,
        t: int | None = None,
    ) -> BusEvent:
        """Publish one event of an attached session and return it as delivered.

        A persisted event is appended to `events.jsonl` first (in a worker thread) and carries the
        `seq` the log gave it; a notice is only delivered. `t` defaults to the session time now.

        Raises:
            SessionNotAttachedError: the session is not attached (unknown, not active, ended).
            SessionEndedError, SecretRefused, ValidationError: from the vault append; nothing is
                delivered and the log's `seq` does not move.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotAttachedError(
                    f"session {session_id!r} is not active on this backend"
                )
            body = dict(payload or {})
            if persist:
                written = await asyncio.to_thread(session.append_event, kind, origin, body, t)
                event = BusEvent(
                    session_id=session.id,
                    subject_id=session.subject_slug,
                    topic_id=session.topic_slug,
                    kind=written.kind,
                    origin=written.origin,
                    t=written.t,
                    payload=written.payload,
                    seq=written.seq,
                    schema_version=written.schema_version,
                )
                if self.on_append is not None:
                    self.on_append()
            else:
                event = BusEvent(
                    session_id=session.id,
                    subject_id=session.subject_slug,
                    topic_id=session.topic_slug,
                    kind=kind,
                    origin=origin,
                    t=_session_time(session) if t is None else t,
                    payload=body,
                )
            for subscription in list(self._subscriptions):
                if subscription.wants(event):
                    subscription.offer(event)
            return event

    # -- subscribe -----------------------------------------------------------------------------

    def subscribe(
        self,
        *,
        name: str = "",
        session_id: str | None = None,
        kinds: Collection[str] | None = None,
        maxsize: int | None = None,
    ) -> Subscription:
        """A new subscription to every event published from now on that matches the filter.

        `session_id` limits it to one session, `kinds` to those event kinds (None: all);
        `maxsize` is its queue bound (default `default_queue_size`); `name` shows up in logs.
        """
        subscription = Subscription(
            self,
            name=name,
            session_id=session_id,
            kinds=kinds,
            maxsize=self.default_queue_size if maxsize is None else maxsize,
        )
        self._subscriptions.append(subscription)
        return subscription

    def _unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions)

    def close(self) -> None:
        """Close every subscription (shutdown); attached sessions are forgotten."""
        for subscription in list(self._subscriptions):
            subscription.close()
        self._sessions.clear()


def _session_time(session: Session) -> int:
    elapsed = datetime.now(UTC) - session.meta.started_at
    return max(0, int(elapsed / timedelta(milliseconds=1)))
