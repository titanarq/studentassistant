"""The workspace hub: everything that happens to a topic's chat and document, live, per topic.

The study workspace (`/subjects/{s}/topics/{t}/workspace`) follows one topic through
`GET .../workspace/stream` (`workspace_routes.py`). What feeds that stream is this in-memory hub
(one per app, `app.state.workspace`): the voice turns of `assistant_requests.py`, the typed turns
of `POST .../notes/chat` (`revise_routes.py`, which still streams to its own caller too), the
student's saves (`notes_edit_routes.py`), "prepárame el tema" (`notes_routes.NotesGenerator`), a
restore (`versions_routes.py`) and an undo publish to it. It works with or without an active
session: it is keyed by topic, not by session, and nothing in it is persisted (the conversation
and the notes are in the vault; a client that reconnects reloads `GET .../notes/chat`).

The events (`WORKSPACE_EVENTS`; the list is open, later tasks add kinds):

- `request.detected` `{request_id, kind, summary, transcript}`: a spoken request was detected and
  queued (`transcript`: `session_id`, `segment_ids`, `t_start_ms`, `t_end_ms`, `text`).
- `turn.started` `{turn_id, request_id|null, origin: typed|voice, kind}`: an editor turn starts
  (`kind` `revise`, `prepare_notes` for "prepárame el tema", or `incorporate` for one
  incorporation of a few sources, #326 -- each batch of a batched "prepárame el tema" is one).
- `reply.delta` `{turn_id, text, attempt}` and `reply.restart` `{turn_id, attempt}`: the reply as
  it is written.
- `turn.result`: the turn's result (`RevisionResult`, `GenerationResult` for `prepare_notes`,
  `IncorporationResult` for `incorporate`) plus `turn_id`, `request_id` and `kind`.
- `turn.error` `{turn_id, request_id, status, detail, code?}`: the turn failed (`status` as the
  same failure over REST; `code` e.g. `cost_cap_reached`).
- `notes.changed` `{revision, origin: editor|user|generation|restore, summary, turn_id?}`: the
  topic's `apuntes.md` changed.
- `doubt.asked` `{pending_id, question, suggestions, options, refs}`: the chat asks one open doubt
  (one at a time, `doubt_chat.py`); `doubt.resolved` `{pending_id, status, resolution,
  notes_changed}`: it was answered or dismissed; `doubts.auto_resolved` `{pending_ids, summary}`:
  the editor settled those doubts from the sources, reported as one short line.
- `incorporation.progress` `{done, total, source_ids}`: a batched "prepárame el tema" finished
  one batch (`source_ids`); `done` of the `total` pending sources are incorporated.

Delivery never waits for a subscriber: each subscription has a bounded queue, and when it is full
the oldest `reply.delta` (else the oldest event) is dropped and counted in `dropped`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self

from pydantic import BaseModel

logger = logging.getLogger(__name__)

REQUEST_DETECTED = "request.detected"
TURN_STARTED = "turn.started"
REPLY_DELTA = "reply.delta"
REPLY_RESTART = "reply.restart"
TURN_RESULT = "turn.result"
TURN_ERROR = "turn.error"
NOTES_CHANGED = "notes.changed"
DOUBT_ASKED = "doubt.asked"
DOUBT_RESOLVED = "doubt.resolved"
DOUBTS_AUTO_RESOLVED = "doubts.auto_resolved"
INCORPORATION_PROGRESS = "incorporation.progress"
WORKSPACE_EVENTS: tuple[str, ...] = (
    REQUEST_DETECTED,
    TURN_STARTED,
    REPLY_DELTA,
    REPLY_RESTART,
    TURN_RESULT,
    TURN_ERROR,
    NOTES_CHANGED,
    DOUBT_ASKED,
    DOUBT_RESOLVED,
    DOUBTS_AUTO_RESOLVED,
    INCORPORATION_PROGRESS,
)
"""The events the workspace stream carries today (open: later tasks add kinds)."""

NotesOrigin = Literal["editor", "user", "generation", "restore"]
TurnKind = Literal["revise", "prepare_notes", "incorporate"]

DEFAULT_QUEUE_SIZE = 1024


class WorkspaceClosedError(Exception):
    """The subscription was closed (the client went away, or the app shuts down)."""


@dataclass(frozen=True)
class WorkspaceEvent:
    """One event of a topic's workspace; `data` belongs to every subscriber: never mutate it."""

    subject_id: str
    topic_id: str
    event: str
    data: Mapping[str, Any]


class WorkspaceSubscription:
    """One client's view of a topic: a bounded queue of its `WorkspaceEvent`s."""

    def __init__(self, hub: WorkspaceHub, subject_id: str, topic_id: str, *, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("a subscription queue holds at least one event")
        self.subject_id, self.topic_id, self.maxsize = subject_id, topic_id, maxsize
        self.dropped = 0
        self._hub = hub
        self._queue: deque[WorkspaceEvent] = deque()
        self._waiter: asyncio.Future[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, event: WorkspaceEvent) -> None:
        if self._closed:
            return
        if len(self._queue) >= self.maxsize:
            self.dropped += 1
            for index, queued in enumerate(self._queue):
                if queued.event == REPLY_DELTA:
                    del self._queue[index]
                    break
            else:
                self._queue.popleft()
        self._queue.append(event)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def get(self) -> WorkspaceEvent:
        """The next event; `WorkspaceClosedError` once closed and drained."""
        while not self._queue:
            if self._closed:
                raise WorkspaceClosedError
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._queue.popleft()

    def drain(self) -> list[WorkspaceEvent]:
        """Everything queued now, without waiting."""
        events: list[WorkspaceEvent] = []
        while self._queue:
            events.append(self._queue.popleft())
        return events

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hub._unsubscribe(self)
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


class WorkspaceHub:
    """Per-topic in-memory publish/subscribe of workspace events (see the module docstring)."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue_size = queue_size
        self._subscriptions: dict[tuple[str, str], list[WorkspaceSubscription]] = {}

    def subscribe(self, subject_id: str, topic_id: str) -> WorkspaceSubscription:
        """A subscription to every event of the topic published from now on."""
        subscription = WorkspaceSubscription(self, subject_id, topic_id, maxsize=self.queue_size)
        self._subscriptions.setdefault((subject_id, topic_id), []).append(subscription)
        return subscription

    def subscribers(self, subject_id: str, topic_id: str) -> int:
        return len(self._subscriptions.get((subject_id, topic_id), []))

    def publish(self, subject_id: str, topic_id: str, event: str, data: Mapping[str, Any]) -> None:
        """Deliver one event to the topic's subscribers (never waits; call in the event loop)."""
        message = WorkspaceEvent(subject_id, topic_id, event, dict(data))
        for subscription in list(self._subscriptions.get((subject_id, topic_id), [])):
            subscription.offer(message)

    def notes_changed(
        self,
        subject_id: str,
        topic_id: str,
        *,
        revision: str | None,
        origin: NotesOrigin,
        summary: str | None,
        turn_id: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"revision": revision, "origin": origin, "summary": summary}
        if turn_id is not None:
            data["turn_id"] = turn_id
        self.publish(subject_id, topic_id, NOTES_CHANGED, data)

    def close(self) -> None:
        """Close every subscription (shutdown): the streams end."""
        for subscriptions in list(self._subscriptions.values()):
            for subscription in list(subscriptions):
                subscription.close()

    def _unsubscribe(self, subscription: WorkspaceSubscription) -> None:
        key = (subscription.subject_id, subscription.topic_id)
        subscriptions = self._subscriptions.get(key)
        if subscriptions and subscription in subscriptions:
            subscriptions.remove(subscription)
            if not subscriptions:
                del self._subscriptions[key]


def new_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex[:16]}"


class TurnBroadcast:
    """Publishes one editor turn to the hub: `started`, the reply, then `result` or `error`."""

    def __init__(
        self,
        hub: WorkspaceHub,
        subject_id: str,
        topic_id: str,
        *,
        origin: Literal["typed", "voice"],
        request_id: str | None = None,
        kind: TurnKind = "revise",
        turn_id: str | None = None,
    ) -> None:
        self.hub, self.subject_id, self.topic_id = hub, subject_id, topic_id
        self.origin, self.request_id, self.kind = origin, request_id, kind
        self.turn_id = turn_id or new_turn_id()

    def _publish(self, event: str, data: Mapping[str, Any]) -> None:
        self.hub.publish(self.subject_id, self.topic_id, event, data)

    def started(self) -> None:
        self._publish(
            TURN_STARTED,
            {
                "turn_id": self.turn_id,
                "request_id": self.request_id,
                "origin": self.origin,
                "kind": self.kind,
            },
        )

    async def reply(self, event: str, data: dict[str, Any]) -> None:
        """A `ReplySink`: `reply.delta` / `reply.restart` with the turn's id."""
        self._publish(event, {**data, "turn_id": self.turn_id})

    def result(self, result: BaseModel) -> None:
        """`turn.result`; for a revision or an incorporation that changed the notes,
        `notes.changed` (`editor`)."""
        payload = result.model_dump(mode="json")
        self._publish(
            TURN_RESULT,
            {**payload, "turn_id": self.turn_id, "request_id": self.request_id, "kind": self.kind},
        )
        if self.kind in ("revise", "incorporate") and payload.get("notes_changed"):
            self.hub.notes_changed(
                self.subject_id,
                self.topic_id,
                revision=payload.get("revision"),
                origin="editor",
                summary=payload.get("summary"),
                turn_id=self.turn_id,
            )

    def error(self, status: int, detail: str, code: str | None = None) -> None:
        data: dict[str, Any] = {
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "status": status,
            "detail": detail,
        }
        if code is not None:
            data["code"] = code
        self._publish(TURN_ERROR, data)


__all__ = [
    "DOUBTS_AUTO_RESOLVED",
    "DOUBT_ASKED",
    "DOUBT_RESOLVED",
    "INCORPORATION_PROGRESS",
    "NOTES_CHANGED",
    "REPLY_DELTA",
    "REPLY_RESTART",
    "REQUEST_DETECTED",
    "TURN_ERROR",
    "TURN_RESULT",
    "TURN_STARTED",
    "WORKSPACE_EVENTS",
    "NotesOrigin",
    "TurnBroadcast",
    "WorkspaceClosedError",
    "WorkspaceEvent",
    "WorkspaceHub",
    "WorkspaceSubscription",
    "new_turn_id",
]
