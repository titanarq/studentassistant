"""Spoken requests to the assistant become editor turns: the `assistant.request` consumer.

The observer's request detector (#314) -- or the wake word (#318) -- publishes an
`assistant.request` session event (`observer.AssistantRequest`) whenever the student addresses the
assistant. `AssistantRequestConsumer` (started in the app's lifespan when the app has an
`llm_transport`) subscribes to that kind on the bus and, **per topic, runs one request at a time in
arrival order**:

- On arrival, the request is announced at once on the topic's workspace stream
  (`request.detected` `{request_id, kind, summary, transcript}`, `workspace.py`) and queued. A
  request arriving while a turn of the topic runs waits in the queue; the queue lives in the
  backend, not in a client, so it survives a client that goes away, and the requests of a session
  that ended meanwhile are still processed (they work on the vault, not the session).
- When its turn comes, it takes the topic's notes lock (`NotesGenerator.claim`), waiting while
  something else holds it (a typed turn, "prepárame el tema", a restore; at most
  `CLAIM_TIMEOUT_SECONDS`, then a `turn.error` 409), then `turn.started` (origin `voice`) and the
  dispatch by kind (`HANDLERS`, a per-kind table later tasks extend):
  - `edit` and `question`: `editor.revise_notes` on the topic's latest notes, the request's raw
    `text` as the message and a `ChatRequestRef` (request id, summary, session, segments, times,
    text) as `request`, so the turn is recorded as a voice chat turn (`GET .../notes/chat`). The
    reply streams (`reply.delta`, `reply.restart`), then `turn.result` (the `RevisionResult`) and,
    when the notes changed, `notes.changed` (origin `editor`); `notes.edited` goes on the bus when
    the session is still active.
  - `prepare_notes`: "prepárame el tema" through the same `NotesGenerator.generate` as the button
    and with the same rule: never past a reached cost cap without the student's confirmation, so a
    reached cap is a `turn.error` with code `cost_cap_reached` (the student confirms through
    `POST .../notes/generate`), never a silent spend. A generation that wrote the notes is a
    `notes.changed` (origin `generation`), then `turn.result` (the `GenerationResult`).
- A failure is a `turn.error` `{turn_id, request_id, status, detail, code?}` with the status the
  same failure has over REST (a reached cap 409 `cost_cap_reached`, a Claude failure 502, ...).
  The next request of the topic runs anyway.

Shutdown (`stop`) closes the bus subscription and gives the running turns
`SHUTDOWN_TIMEOUT_SECONDS` to finish before cancelling them; still-queued requests are dropped
with a log line (they stay in the session's `events.jsonl`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from studentassistant.editor.revise import ChatRequestRef, revise_notes
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.observer import ASSISTANT_REQUEST_KIND, AssistantRequest
from studentassistant.server.bus import BusError, SessionBus, Subscription
from studentassistant.server.doubt_chat import DoubtChat
from studentassistant.server.errors import cost_cap_error
from studentassistant.server.notes_routes import (
    CONFIRM_SENTENCE,
    ERROR_DETAIL,
    FAILED_DETAIL,
    REFUSED_DETAIL,
    TURN_HOLDER,
    NotesGenerator,
)
from studentassistant.server.revise_routes import turn_error
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.server.workspace import REQUEST_DETECTED, TurnBroadcast, WorkspaceHub

logger = logging.getLogger(__name__)

CLAIM_POLL_SECONDS = 0.1
CLAIM_TIMEOUT_SECONDS = 600.0
"""How long a request waits for the topic's notes lock before it fails as busy."""
SHUTDOWN_TIMEOUT_SECONDS = 5.0

BUSY_DETAIL = (
    "No se ha podido atender la petición: el editor lleva demasiado tiempo ocupado con los"
    " apuntes de este tema."
)
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."


class _BusyError(Exception):
    """The topic's notes lock stayed taken for `claim_timeout` seconds."""


@dataclass(frozen=True)
class QueuedRequest:
    """One request waiting for its turn."""

    subject_id: str
    topic_id: str
    session_id: str
    request: AssistantRequest

    def reference(self) -> ChatRequestRef:
        return ChatRequestRef(
            request_id=self.request.request_id,
            summary=self.request.summary,
            session_id=self.session_id,
            segment_ids=list(self.request.segment_ids),
            t_start_ms=self.request.t_start_ms,
            t_end_ms=self.request.t_end_ms,
            text=self.request.text,
        )


Handler = Callable[["AssistantRequestConsumer", QueuedRequest, TurnBroadcast], Awaitable[BaseModel]]


class AssistantRequestConsumer:
    """Runs each topic's spoken requests as editor turns, in order (see the module docstring)."""

    def __init__(
        self,
        bus: SessionBus,
        sessions: SessionService,
        generator: NotesGenerator,
        hub: WorkspaceHub,
        *,
        claim_poll: float = CLAIM_POLL_SECONDS,
        claim_timeout: float = CLAIM_TIMEOUT_SECONDS,
        doubts: DoubtChat | None = None,
    ) -> None:
        self.bus, self.sessions, self.generator, self.hub = bus, sessions, generator, hub
        self.doubts = doubts
        """Asks the next doubt after a turn that changed the notes (#325); None: nothing asked."""
        self.claim_poll, self.claim_timeout = claim_poll, claim_timeout
        self._subscription: Subscription | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queues: dict[tuple[str, str], deque[QueuedRequest]] = {}
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to `assistant.request` and start reading (in the running event loop)."""
        if self._reader is not None:
            return
        self._subscription = self.bus.subscribe(
            name="assistant-requests", kinds={ASSISTANT_REQUEST_KIND}
        )
        self._reader = asyncio.create_task(self._read(), name="assistant-requests")

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        if self._subscription is not None:
            self._subscription.close()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        for key, queue in self._queues.items():
            if queue:
                logger.warning(
                    "dropping %d queued assistant requests of %s/%s at shutdown", len(queue), *key
                )
            queue.clear()
        workers = set(self._workers.values())
        if workers:
            _done, pending = await asyncio.wait(workers, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Wait until every queue is empty and no turn runs (tests)."""
        deadline = time.monotonic() + timeout
        while self._workers or any(self._queues.values()):
            if time.monotonic() > deadline:
                raise TimeoutError("the assistant requests did not finish")
            await asyncio.sleep(0.01)

    def queued(self, subject_id: str, topic_id: str) -> int:
        return len(self._queues.get((subject_id, topic_id), ()))

    # -- intake ----------------------------------------------------------------------------------

    async def _read(self) -> None:
        assert self._subscription is not None
        while True:
            try:
                event = await self._subscription.get()
            except BusError:
                return
            try:
                request = AssistantRequest.model_validate(dict(event.payload))
            except ValidationError:
                logger.warning(
                    "ignoring a malformed assistant.request of session %s", event.session_id
                )
                continue
            self.submit(event.subject_id, event.topic_id, event.session_id, request)

    def submit(
        self, subject_id: str, topic_id: str, session_id: str, request: AssistantRequest
    ) -> None:
        """Announce one request on the workspace stream and queue it for its topic."""
        queued = QueuedRequest(subject_id, topic_id, session_id, request)
        reference = queued.reference()
        self.hub.publish(
            subject_id,
            topic_id,
            REQUEST_DETECTED,
            {
                "request_id": request.request_id,
                "kind": request.kind,
                "summary": request.summary,
                "transcript": reference.model_dump(
                    mode="json",
                    include={"session_id", "segment_ids", "t_start_ms", "t_end_ms", "text"},
                ),
            },
        )
        key = (subject_id, topic_id)
        self._queues.setdefault(key, deque()).append(queued)
        if key not in self._workers:
            task = asyncio.create_task(
                self._work(key), name=f"assistant-requests-{subject_id}-{topic_id}"
            )
            self._workers[key] = task

    # -- the turns -------------------------------------------------------------------------------

    async def _work(self, key: tuple[str, str]) -> None:
        try:
            queue = self._queues[key]
            while queue:
                queued = queue.popleft()
                try:
                    await self._run(queued)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - `_run` reports its own failures
                    logger.exception("an assistant request of %s/%s failed", *key)
        finally:
            self._workers.pop(key, None)
            if not self._queues.get(key):
                self._queues.pop(key, None)

    async def _run(self, queued: QueuedRequest) -> None:
        request = queued.request
        handler = HANDLERS.get(request.kind)
        if handler is None:
            logger.warning("no handler for assistant request kind %r", request.kind)
            return
        kind = "prepare_notes" if request.kind == "prepare_notes" else "revise"
        broadcast = TurnBroadcast(
            self.hub,
            queued.subject_id,
            queued.topic_id,
            origin="voice",
            request_id=request.request_id,
            kind=kind,
        )
        holder = "editor" if kind == "prepare_notes" else TURN_HOLDER
        try:
            await self._claim(queued.subject_id, queued.topic_id, holder)
        except _BusyError:
            broadcast.error(409, BUSY_DETAIL)
            return
        try:
            broadcast.started()
            result = await handler(self, queued, broadcast)
            broadcast.result(result)
            if self.doubts is not None:
                self.doubts.after(queued.subject_id, queued.topic_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status, detail, code = _error_of(error, kind)
            if status >= 500:
                logger.warning(
                    "the assistant request %s of %s/%s failed: %s",
                    request.request_id,
                    queued.subject_id,
                    queued.topic_id,
                    error,
                    exc_info=status == 500,
                )
            broadcast.error(status, detail, code)
        finally:
            self.generator.release(queued.subject_id, queued.topic_id)

    async def _claim(self, subject_id: str, topic_id: str, holder: str) -> None:
        deadline = time.monotonic() + self.claim_timeout
        while not self.generator.claim(subject_id, topic_id, holder):
            if time.monotonic() >= deadline:
                raise _BusyError
            await asyncio.sleep(self.claim_poll)

    def _publisher(self, subject_id: str, topic_id: str) -> Callable[..., Awaitable[None]]:
        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = self.sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await self.bus.publish(active.session_id, kind, "editor", payload)

        return publish

    async def _revise(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        vault = await self.sessions.open_vault()
        sync = self.sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise VaultUnavailableError("the vault has no sync")
        client = get_client(
            "editor",
            settings=self.generator.settings,
            transport=self.generator.transport,
            ledger=LedgerBinding(vault, queued.subject_id, queued.topic_id),
        )
        return await revise_notes(
            vault,
            queued.subject_id,
            queued.topic_id,
            queued.request.text,
            client=client,
            sync=sync,
            on_reply=broadcast.reply,
            on_event=self._publisher(queued.subject_id, queued.topic_id),
            request=queued.reference(),
            turn_id=broadcast.turn_id,
            live=None
            if self.doubts is None
            else self.doubts.live(queued.subject_id, queued.topic_id),
            host=self.sessions.host,
        )

    async def _prepare(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        # Never `confirm_over_cap`: a reached cap is a `turn.error` the student confirms.
        return await self.generator.generate(self.sessions, queued.subject_id, queued.topic_id)


HANDLERS: Mapping[str, Handler] = {
    "edit": AssistantRequestConsumer._revise,
    "question": AssistantRequestConsumer._revise,
    "prepare_notes": AssistantRequestConsumer._prepare,
}
"""What runs each request kind (later tasks add kinds here)."""


def _error_of(error: BaseException, kind: str) -> tuple[int, str, str | None]:
    """`(status, detail, code)` of a failed turn, as its REST error would have been."""
    if isinstance(error, VaultUnavailableError):
        return 503, VAULT_UNAVAILABLE_DETAIL, None
    if kind == "prepare_notes":
        if isinstance(error, CostConfirmationRequiredError):
            refused = cost_cap_error(error, CONFIRM_SENTENCE)
            code = refused.code
            return refused.status_code, refused.detail, None if code is None else code.value
        if isinstance(error, RefusalError):
            return 502, REFUSED_DETAIL, None
        if isinstance(error, LLMError):
            return 502, FAILED_DETAIL, None
        return 500, ERROR_DETAIL, None
    status, detail, error_code = turn_error(error)
    return status, detail, None if error_code is None else error_code.value


__all__ = ["HANDLERS", "AssistantRequestConsumer", "QueuedRequest"]
