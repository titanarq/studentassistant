"""Requests to the assistant become editor turns: the `assistant.request` consumer.

The observer's request detector (#314) -- or the wake word (#318) -- publishes an
`assistant.request` session event (`observer.AssistantRequest`) whenever the student addresses the
assistant; a message typed in the workspace chat (`post_message`, `POST .../workspace/messages`,
#327) is classified by the same prompt and becomes the same events (`detector: "typed"`).
`AssistantRequestConsumer` (started in the app's lifespan when the app has an `llm_transport`)
subscribes to that kind on the bus and, **per topic, runs one request at a time in arrival
order**:

- On arrival, the request is announced at once on the topic's workspace stream
  (`request.detected` `{request_id, kind, summary, origin, transcript, targets?}`,
  `workspace.py`) and queued. A
  request arriving while a turn of the topic runs waits in the queue; the queue lives in the
  backend, not in a client, so it survives a client that goes away, and the requests of a session
  that ended meanwhile are still processed (they work on the vault, not the session).
- When its turn comes, it takes the topic's notes lock (`NotesGenerator.claim`), waiting while
  something else holds it (a typed turn, "prepárame el tema", a restore; at most
  `CLAIM_TIMEOUT_SECONDS`, then a `turn.error` 409), then `turn.started` (origin `voice`, or
  `typed` for a typed request) and the dispatch by kind (`HANDLERS`):
  - `edit` and `question`: `editor.revise_notes` on the topic's latest notes, the request's raw
    `text` as the message and a `ChatRequestRef` (request id, summary, session, segments, times,
    text) as `request`, so the turn is recorded as a voice chat turn (`GET .../notes/chat`); a
    typed request is a typed turn (no `request`). The
    reply streams (`reply.delta`, `reply.restart`), then `turn.result` (the `RevisionResult`) and,
    when the notes changed, `notes.changed` (origin `editor`); `notes.edited` goes on the bus when
    the session is still active.
  - `prepare_notes`: "prepárame el tema" through the same `NotesGenerator.generate` as the button
    and with the same rule: never past a reached cost cap without the student's confirmation, so a
    reached cap is a `turn.error` with code `cost_cap_reached` (the student confirms it, below),
    never a silent spend. A generation that wrote the notes is a
    `notes.changed` (origin `generation`), then `turn.result` (the `GenerationResult`); in the
    batched mode (#326) each batch is its own `incorporate` turn on the stream too.
  - `incorporate` (#327): `NotesGenerator.incorporate` of the request's `targets` (one small
    editor call, #326), a turn of kind `incorporate` (`turn.result` the `IncorporationResult`).
  - `set_aside` / `restore`: `sources.set_capture_triage` per target (a target already in that
    state is left alone), a `capture.triaged` event (origin `user`; in the topic's live session,
    which makes the transcriber transcribe a restored page with no transcription, else in a review
    session) per change, and one short chat entry streamed as the reply and recorded as a
    `triage` chat turn («He apartado la página 3.», «He recuperado la página 3; se está
    transcribiendo.»); `notes.changed` untouched. `turn.result` is the `TriageTurn`.
  - `doubt_answer`: `editor.answer_doubt(pending_id, answer)` (#325; a number is the suggestion
    picked, anything else the answer's words), the resolution streamed as the reply, then
    `doubt.resolved` (and `notes.changed` when the notes changed) through the `DoubtChat`, which
    asks the next doubt; `turn.result` is the `ResolutionResult`.
  - `study` (#335): "ya está, quiero estudiar" -- `study_routes.switch_to_study`, the service of
    `POST .../study`: the topic's capture session is ended (without "prepárame el tema") and the
    notes are labelled "versión de estudio" (`study.marked` on the stream). One line streamed as
    the reply («He cerrado la captura y marcado los apuntes v3 como versión de estudio.»);
    `turn.result` is the `StudyTurn`, whose `action` `{kind: "go_study", path}` the web shows as
    an "Ir a Estudiar" button. Not recorded in the chat history. No notes is a `turn.error` 409.
- A failure is a `turn.error` `{turn_id, request_id, status, detail, code?}` with the status the
  same failure has over REST (a reached cap 409 `cost_cap_reached`, a Claude failure 502, ...).
  The next request of the topic runs anyway.
- A request stopped at the cost cap is kept (in memory, the last `STOPPED_PER_TOPIC` of each
  topic, keyed by the failed turn's `turn_id`) so the student can confirm it: `confirm` (`POST
  .../workspace/messages` `{confirm_over_cap: true, turn_id}`, #351) queues the same, already
  classified request again with `confirm_over_cap` threaded to its handler (`revise_notes`,
  `NotesGenerator.generate`, `NotesGenerator.incorporate`, `answer_doubt`); it is not
  classified again and not announced again (no second `request.detected`), and its new turn
  carries the same `request_id`. A request is confirmed once; after a restart nothing is kept.

Surviving a restart (#408): every request that ran to a result or an error is recorded as a
persisted `turn.finished` event (`TURN_FINISHED_KIND`, origin `editor`, `{request_id, session_id,
turn_id, kind, outcome: "result" | "error", status?, code?}`) in the request's session while that
session is attached. When a session is started or resumed on the bus (and at `start`, for a session
already attached), `replay` reads the session's `events.jsonl` and queues again, through `submit`,
oldest first, each `assistant.request` no turn answered (`unanswered_requests`): no `turn.finished`
for its id, no voice chat turn (`editor.chat_history`) whose transcript is that request, and after
the newest answered request of the session (requests run in order, so what precedes an answered one
ran). Each `(session, request_id)` is submitted at most once per process, so a request queued or
running now, or replayed already, is never queued twice. What is not replayed: a typed request kept
in a review session (no capture session was open), and a request whose session ended before its
turn ran -- those are the outstanding ones below.

Requests of an ended session (#423): a typed request kept in a review session (no capture session
was open) and a request whose session ended before its turn ran cannot wait for a resume. They
are recorded as outstanding in a `requests.outstanding` event (`REQUESTS_OUTSTANDING_KIND`,
origin `editor`, `{session_id?, request_ids}`; `session_id` omitted: the event's own session)
of a review session: `post_message` writes it next to the typed requests of its review session,
and a `session.ended` on the bus writes it (in a new review session) for the ended session's
requests still queued or running. A `turn.finished` whose session is no longer attached is
written in a review session too, with the request's `session_id` in it. Once the vault is open
(`catch_up_vault`, a `SessionService.add_on_open` hook), every topic's outstanding requests with
no `turn.finished` (`outstanding_requests`) are submitted again, oldest first, so each is
answered after a restart; submitted once per process, like every request.

Shutdown (`stop`) closes the bus subscription and gives the running turns
`SHUTDOWN_TIMEOUT_SECONDS` to finish before cancelling them; still-queued requests, and the
cancelled turns, have no `turn.finished`, so they are replayed when their session is resumed, or
at the next start for an outstanding one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from studentassistant.editor.doubts import (
    DoubtAnswer,
    DoubtClosedError,
    DoubtError,
    InvalidAnswerError,
    OpenSessionError,
    UnknownDoubtError,
    answer_doubt,
    doubt_chat_turns,
)
from studentassistant.editor.incorporate import IncorporationError, SourceStatus, source_status
from studentassistant.editor.revise import (
    REPLY_DELTA,
    ChatRequestRef,
    ChatTurn,
    TriageTarget,
    TriageTurn,
    chat_history,
    record_triage_turn,
    revise_notes,
)
from studentassistant.editor.versions import VersionError
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.observer import (
    ASSISTANT_REQUEST_KIND,
    AskedDoubtRef,
    AssistantRequest,
    RequestContext,
    RequestSource,
    SourcesLookup,
)
from studentassistant.observer.assistant_request import SUMMARY_MAX_CHARS, TYPED_REQUEST_PREFIX
from studentassistant.observer.requests import (
    MessageClassifier,
    ReportedRequest,
)
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.bus import BusError, BusEvent, SessionBus, Subscription
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
from studentassistant.server.sessions import (
    SESSION_ENDED,
    SESSION_RESUMED,
    SESSION_STARTED,
    SessionService,
    VaultUnavailableError,
)
from studentassistant.server.study_routes import (
    GoStudyAction,
    StudyTurn,
    study_path,
    study_reply,
    switch_to_study,
)
from studentassistant.server.workspace import (
    REQUEST_DETECTED,
    TurnBroadcast,
    TurnKind,
    WorkspaceHub,
)
from studentassistant.sources import triage_status
from studentassistant.sources.catchup import readable_topics
from studentassistant.sources.triage import (
    CAPTURE_TRIAGED_KIND,
    REASON_TEXT,
    TRIAGE_REASONS,
    TriageReason,
    TriageResult,
    change_for,
    set_capture_triage,
    triaged_payload,
)
from studentassistant.vault import (
    Event,
    Origin,
    SourceError,
    Vault,
    end_session,
    list_sessions,
    read_source,
    read_topic_events,
    start_session,
)

logger = logging.getLogger(__name__)

CLAIM_POLL_SECONDS = 0.1
CLAIM_TIMEOUT_SECONDS = 600.0
"""How long a request waits for the topic's notes lock before it fails as busy."""
SHUTDOWN_TIMEOUT_SECONDS = 5.0
STOPPED_PER_TOPIC = 32
"""How many requests stopped at the cost cap each topic keeps for a confirmation."""

BUSY_DETAIL = (
    "No se ha podido atender la petición: el editor lleva demasiado tiempo ocupado con los"
    " apuntes de este tema."
)
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_SOURCE_DETAIL = "No encuentro esa página entre las fuentes del tema."
UNKNOWN_KIND_DETAIL = "Todavía no sé atender este tipo de petición: pídemelo de otra forma."
NOT_STOPPED_DETAIL = (
    "Esa petición ya no está esperando confirmación (quizá ya se hizo o el servidor se reinició):"
    " vuelve a pedirla."
)

TURN_KINDS: Mapping[str, TurnKind] = {
    "edit": "revise",
    "question": "revise",
    "prepare_notes": "prepare_notes",
    "incorporate": "incorporate",
    "set_aside": "set_aside",
    "restore": "restore",
    "doubt_answer": "doubt_answer",
    "study": "study",
}
"""The workspace turn kind of each request kind."""
TURN_FINISHED_KIND = "turn.finished"
"""The persisted record of a request's turn that ended in a result or an error (#408)."""
REPLAY_TRIGGERS = frozenset({SESSION_STARTED, SESSION_RESUMED})
"""The bus events after which the consumer replays a session's unanswered requests."""
REQUESTS_OUTSTANDING_KIND = "requests.outstanding"
"""The requests of an ended session still to run, recorded in a review session (#423)."""
TYPED_ORIGIN: Origin = "user"
"""The origin of a typed message's `assistant.request` and of a student's `capture.triaged`."""


class _BusyError(Exception):
    """The topic's notes lock stayed taken for `claim_timeout` seconds."""


class NotStoppedError(LookupError):
    """`confirm` of a turn that is not a request stopped at the cost cap (any more)."""


@dataclass(frozen=True)
class QueuedRequest:
    """One request waiting for its turn."""

    subject_id: str
    topic_id: str
    session_id: str
    request: AssistantRequest
    confirm_over_cap: bool = False
    """The student confirmed going past a reached cost cap (`AssistantRequestConsumer.confirm`)."""

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

    def chat_request(self) -> ChatRequestRef | None:
        """The spoken request an editor turn answers; `None` for a typed one (a typed turn)."""
        return None if self.request.typed else self.reference()


class TypedMessageResult(BaseModel):
    """The answer of `POST .../workspace/messages`: the message and the requests queued for it."""

    message_id: str
    requests: list[AssistantRequest]
    classified: bool = True
    """False when the classifier failed and the message was kept as one `edit`/`question`."""


Handler = Callable[["AssistantRequestConsumer", QueuedRequest, TurnBroadcast], Awaitable[BaseModel]]


class AssistantRequestConsumer:
    """Runs each topic's requests as editor turns, in order (see the module docstring)."""

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
        transcribing: bool = False,
    ) -> None:
        self.bus, self.sessions, self.generator, self.hub = bus, sessions, generator, hub
        self.doubts = doubts
        """Asks the next doubt after a turn that changed the notes (#325); None: nothing asked."""
        self.transcribing = transcribing
        """Whether the app transcribes captures (a restored page is then transcribed at once)."""
        self.claim_poll, self.claim_timeout = claim_poll, claim_timeout
        self._subscription: Subscription | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queues: dict[tuple[str, str], deque[QueuedRequest]] = {}
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._typed_lock = asyncio.Lock()
        self._typed_counts: dict[str, int] = {}
        self._stopped: dict[tuple[str, str], dict[str, QueuedRequest]] = {}
        """Per topic, the requests stopped at the cost cap by their failed turn id, oldest first."""
        self._busy = False
        """The reader is handling an event (`wait_idle`)."""
        self._seen: set[tuple[str, str]] = set()
        """`(session_id, request_id)` of every request submitted by this process (#408)."""
        self._running: dict[tuple[str, str], QueuedRequest] = {}
        """Per topic, the request whose turn runs now."""
        self._catch_up: asyncio.Task[None] | None = None
        """The start-up submission of the vault's outstanding requests (#423)."""
        self._stopping = False

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to `assistant.request` and start reading (in the running event loop)."""
        if self._reader is not None:
            return
        self._subscription = self.bus.subscribe(
            name="assistant-requests",
            kinds={ASSISTANT_REQUEST_KIND, SESSION_ENDED, *REPLAY_TRIGGERS},
        )
        self._reader = asyncio.create_task(self._read(), name="assistant-requests")

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        self._stopping = True
        if self._subscription is not None:
            self._subscription.close()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._catch_up is not None:
            self._catch_up.cancel()
            await asyncio.gather(self._catch_up, return_exceptions=True)
        for key, queue in self._queues.items():
            if queue:
                logger.warning(
                    "%d queued assistant requests of %s/%s wait for the session's resume at"
                    " shutdown (%s)",
                    len(queue),
                    *key,
                    ", ".join(queued.request.request_id for queued in queue),
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
        """Wait until the bus events delivered so far are read, every queue is empty and no turn
        runs (tests)."""
        deadline = time.monotonic() + timeout
        while (
            self._reading()
            or (self._catch_up is not None and not self._catch_up.done())
            or self._workers
            or any(self._queues.values())
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("the assistant requests did not finish")
            await asyncio.sleep(0.01)

    def _reading(self) -> bool:
        """Whether the reader has delivered events still to read, or is handling one."""
        if self._reader is None or self._reader.done():
            return False
        return self._busy or (self._subscription is not None and len(self._subscription) > 0)

    def queued(self, subject_id: str, topic_id: str) -> int:
        return len(self._queues.get((subject_id, topic_id), ()))

    # -- intake ----------------------------------------------------------------------------------

    async def _read(self) -> None:
        assert self._subscription is not None
        active = self.sessions.active
        if active is not None:  # attached before we subscribed: its requests may wait already
            self._busy = True
            try:
                await self.replay(active.session_id)
            finally:
                self._busy = False
        while True:
            try:
                event = await self._subscription.get()
            except BusError:
                return
            self._busy = True
            try:
                await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Like every bus subscriber: one bad event never ends the reader.
                logger.exception(
                    "the assistant requests consumer failed on a %s event of session %s",
                    event.kind,
                    event.session_id,
                )
            finally:
                self._busy = False

    async def _on_event(self, event: BusEvent) -> None:
        if event.kind in REPLAY_TRIGGERS:
            await self.replay(event.session_id)
            return
        if event.kind == SESSION_ENDED:
            await self._ended(event.subject_id, event.topic_id, event.session_id)
            return
        try:
            request = AssistantRequest.model_validate(dict(event.payload))
        except ValidationError:
            logger.warning("ignoring a malformed assistant.request of session %s", event.session_id)
            return
        self.submit(event.subject_id, event.topic_id, event.session_id, request)

    async def replay(self, session_id: str) -> list[str]:
        """Queue again the attached session's requests no turn answered (#408), oldest first,
        through `submit` (announced as usual); the ids queued. A session not attached, or one
        whose logs cannot be read (logged), replays nothing."""
        session = self.bus.attached(session_id)
        if session is None:
            return []
        s, t = session.subject_slug, session.topic_slug
        try:
            events = await asyncio.to_thread(lambda: list(session.read_events()))
            turns = (await asyncio.to_thread(chat_history, session.vault, s, t)).turns
        except Exception:
            logger.exception("the requests of session %s cannot be read for a replay", session_id)
            return []
        replayed: list[str] = []
        for request in unanswered_requests(events, session_id, turns):
            if self.submit(s, t, session_id, request):
                replayed.append(request.request_id)
        if replayed:
            logger.info(
                "session %s: replaying %d unanswered assistant requests (%s)",
                session_id,
                len(replayed),
                ", ".join(replayed),
            )
        return replayed

    async def _ended(self, subject_id: str, topic_id: str, session_id: str) -> None:
        """Record the ended session's requests still queued or running as outstanding (#423)."""
        key = (subject_id, topic_id)
        waiting = [
            *([self._running[key]] if key in self._running else []),
            *self._queues.get(key, ()),
        ]
        ids = list(
            dict.fromkeys(q.request.request_id for q in waiting if q.session_id == session_id)
        )
        if not ids:
            return
        payload = {"session_id": session_id, "request_ids": ids}
        await self._write_review(
            subject_id, topic_id, [(REQUESTS_OUTSTANDING_KIND, "editor", payload)]
        )

    async def _write_review(
        self, subject_id: str, topic_id: str, events: list[tuple[str, Origin, dict[str, Any]]]
    ) -> str:
        vault = await self.sessions.open_vault()
        review = await asyncio.to_thread(
            _review_session, vault, subject_id, topic_id, self.sessions.host, events
        )
        if self.sessions.sync is not None:
            self.sessions.sync.note_change()
        return review

    def catch_up_vault(self, vault: Vault) -> None:
        """Once the vault is open: submit every topic's outstanding requests (#423), once per
        process, in the background (a `SessionService.add_on_open` hook)."""
        if self._catch_up is not None or self._stopping:
            return
        self._catch_up = asyncio.create_task(
            self._catch_up_vault(vault), name="assistant-requests:startup"
        )

    async def _catch_up_vault(self, vault: Vault) -> None:
        try:
            topics = await asyncio.to_thread(readable_topics, vault)
        except Exception:
            logger.exception("the outstanding assistant requests cannot be listed")
            return
        for subject_id, topic_id in topics:
            try:
                pending = await asyncio.to_thread(outstanding_requests, vault, subject_id, topic_id)
            except Exception:
                logger.exception(
                    "the outstanding assistant requests of %s/%s cannot be read",
                    subject_id,
                    topic_id,
                )
                continue
            submitted = [
                request.request_id
                for session_id, request in pending
                if self.submit(subject_id, topic_id, session_id, request)
            ]
            if submitted:
                logger.info(
                    "%s/%s: running %d outstanding assistant requests of ended sessions (%s)",
                    subject_id,
                    topic_id,
                    len(submitted),
                    ", ".join(submitted),
                )

    def submit(
        self, subject_id: str, topic_id: str, session_id: str, request: AssistantRequest
    ) -> bool:
        """Announce one request on the workspace stream and queue it for its topic; False (and
        nothing done) when this process has submitted it already."""
        seen = (session_id, request.request_id)
        if seen in self._seen:
            return False
        self._seen.add(seen)
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
                "origin": "typed" if request.typed else "voice",
                "transcript": reference.model_dump(
                    mode="json",
                    include={"session_id", "segment_ids", "t_start_ms", "t_end_ms", "text"},
                ),
                **({"targets": list(request.targets)} if request.targets else {}),
            },
        )
        self._enqueue(queued)
        return True

    def _enqueue(self, queued: QueuedRequest) -> None:
        key = (queued.subject_id, queued.topic_id)
        self._queues.setdefault(key, deque()).append(queued)
        if key not in self._workers:
            task = asyncio.create_task(
                self._work(key), name=f"assistant-requests-{key[0]}-{key[1]}"
            )
            self._workers[key] = task

    # -- the turns -------------------------------------------------------------------------------

    async def _work(self, key: tuple[str, str]) -> None:
        try:
            queue = self._queues[key]
            while queue:
                queued = queue.popleft()
                self._running[key] = queued
                try:
                    await self._run(queued)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - `_run` reports its own failures
                    logger.exception("an assistant request of %s/%s failed", *key)
                finally:
                    self._running.pop(key, None)
        finally:
            self._workers.pop(key, None)
            if not self._queues.get(key):
                self._queues.pop(key, None)

    async def _run(self, queued: QueuedRequest) -> None:
        request = queued.request
        handler = HANDLERS.get(request.kind)
        kind = TURN_KINDS.get(request.kind, "revise")
        broadcast = TurnBroadcast(
            self.hub,
            queued.subject_id,
            queued.topic_id,
            origin="typed" if request.typed else "voice",
            request_id=request.request_id,
            kind=kind,
        )
        if handler is None:
            logger.warning("no handler for assistant request kind %r", request.kind)
            broadcast.error(422, UNKNOWN_KIND_DETAIL)
            await self._finished(queued, broadcast, "error", 422)
            return
        holder = "editor" if kind == "prepare_notes" else TURN_HOLDER
        try:
            await self._claim(queued.subject_id, queued.topic_id, holder)
        except _BusyError:
            broadcast.error(409, BUSY_DETAIL)
            await self._finished(queued, broadcast, "error", 409)
            return
        try:
            broadcast.started()
            result = await handler(self, queued, broadcast)
            # Recorded before it is announced: a turn cut off after its work never runs twice.
            await self._finished(queued, broadcast, "result")
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
            if code == ErrorCode.COST_CAP_REACHED.value:
                self._remember_stopped(broadcast.turn_id, queued)
            await self._finished(queued, broadcast, "error", status, code)
            broadcast.error(status, detail, code)
        finally:
            self.generator.release(queued.subject_id, queued.topic_id)

    async def _finished(
        self,
        queued: QueuedRequest,
        broadcast: TurnBroadcast,
        outcome: Literal["result", "error"],
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        """Persist `turn.finished` in the request's session, so a restart does not replay it;
        for a session no longer attached (ended, a review session), in a review session (#423)."""
        payload: dict[str, Any] = {
            "request_id": queued.request.request_id,
            "session_id": queued.session_id,
            "turn_id": broadcast.turn_id,
            "kind": broadcast.kind,
            "outcome": outcome,
        }
        if status is not None:
            payload["status"] = status
        if code is not None:
            payload["code"] = code
        try:
            try:
                await self.bus.publish(queued.session_id, TURN_FINISHED_KIND, "editor", payload)
            except BusError:  # not attached: an ended session, or a review session
                await self._write_review(
                    queued.subject_id, queued.topic_id, [(TURN_FINISHED_KIND, "editor", payload)]
                )
        except Exception as error:  # the log refused it: the request may be replayed once
            logger.warning(
                "the turn of request %s of session %s was not recorded: %s",
                queued.request.request_id,
                queued.session_id,
                error,
            )

    def _remember_stopped(self, turn_id: str, queued: QueuedRequest) -> None:
        stopped = self._stopped.setdefault((queued.subject_id, queued.topic_id), {})
        stopped[turn_id] = queued
        while len(stopped) > STOPPED_PER_TOPIC:
            del stopped[next(iter(stopped))]

    def confirm(self, subject_id: str, topic_id: str, turn_id: str) -> TypedMessageResult:
        """Queue again, confirmed past the cost cap, the request whose turn `turn_id` stopped at
        the cap (#351): the same request, not classified again; its new turn streams as usual.

        Raises:
            NotStoppedError: `turn_id` is not a request of the topic stopped at the cap (never
                was, already confirmed, or forgotten: a restart, or `STOPPED_PER_TOPIC` newer ones).
        """
        stopped = self._stopped.get((subject_id, topic_id), {})
        queued = stopped.pop(turn_id, None)
        if queued is None:
            raise NotStoppedError(turn_id)
        self._enqueue(dataclasses.replace(queued, confirm_over_cap=True))
        request = queued.request
        return TypedMessageResult(
            message_id=request.message_id or f"msg-{uuid.uuid4().hex[:16]}", requests=[request]
        )

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
            request=queued.chat_request(),
            confirm_over_cap=queued.confirm_over_cap,
            turn_id=broadcast.turn_id,
            live=None
            if self.doubts is None
            else self.doubts.live(queued.subject_id, queued.topic_id),
            host=self.sessions.host,
        )

    async def _prepare(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        # Never past a reached cap unconfirmed: that is a `turn.error` the student confirms.
        return await self.generator.generate(
            self.sessions,
            queued.subject_id,
            queued.topic_id,
            confirm_over_cap=queued.confirm_over_cap,
        )

    async def _incorporate(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        return await self.generator.incorporate(
            self.sessions,
            queued.subject_id,
            queued.topic_id,
            list(queued.request.targets),
            on_reply=broadcast.reply,
            request=queued.chat_request(),
            turn_id=broadcast.turn_id,
            confirm_over_cap=queued.confirm_over_cap,
        )

    async def _set_aside(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        return await self._triage(queued, broadcast, "set_aside")

    async def _restore(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        return await self._triage(queued, broadcast, "restore")

    async def _triage(
        self,
        queued: QueuedRequest,
        broadcast: TurnBroadcast,
        decision: Literal["set_aside", "restore"],
    ) -> BaseModel:
        """Set the request's targets aside (or restore them), one `capture.triaged` each."""
        vault = await self.sessions.open_vault()
        sync = self.sessions.sync
        s, t = queued.subject_id, queued.topic_id
        rows = {row.source_id: row for row in await asyncio.to_thread(source_status, vault, s, t)}
        triage = (
            await asyncio.to_thread(triage_status, vault, s, t) if decision == "set_aside" else {}
        )
        done: list[SourceStatus] = []
        unchanged: list[SourceStatus] = []
        owed: list[SourceStatus] = []
        targets: dict[str, TriageTarget] = {}
        for target in queued.request.targets:
            row = rows.get(target)
            if row is None:
                raise _UnknownSourceError(target)
            already = (row.state == "apartada") == (decision == "set_aside")
            reason: str | None = None
            if decision == "set_aside":
                targets[target] = _target(target, triage.get(target), already=already)
                reasons = targets[target].reasons
                # A flagged page ("puede estar cortada") keeps its reason once set aside.
                reason = reasons[0] if len(reasons) == 1 else None
            if already:
                unchanged.append(row)
                continue
            result = await asyncio.to_thread(
                set_capture_triage, vault, s, t, target, decision, reason=reason, sync=sync
            )
            change = await asyncio.to_thread(change_for, vault, s, t, target, result)
            await self._write_event(
                vault, s, t, CAPTURE_TRIAGED_KIND, TYPED_ORIGIN, triaged_payload(change)
            )
            done.append(row)
            if decision == "restore" and not await asyncio.to_thread(
                _has_transcription, vault, change.source_path
            ):
                owed.append(row)
        live = self._live_session(s, t) is not None
        reply = _triage_reply(
            decision,
            done,
            unchanged,
            owed,
            transcribing=self.transcribing and live,
            targets=targets,
        )
        await broadcast.reply(REPLY_DELTA, {"text": reply, "attempt": 1})
        turn = TriageTurn(
            turn_id=broadcast.turn_id,
            origin=broadcast.origin,
            request=queued.chat_request(),
            decision=decision,
            source_ids=[row.source_id for row in done],
            message=queued.request.text,
            reply=reply,
            applied=bool(done),
            targets=list(targets.values()),
        )
        await asyncio.to_thread(record_triage_turn, vault, s, t, turn)
        if sync is not None:
            sync.note_change()
        return turn

    async def _doubt_answer(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        request = queued.request
        assert request.pending_id is not None and request.answer is not None
        vault = await self.sessions.open_vault()
        sync = self.sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise VaultUnavailableError("the vault has no sync")
        s, t = queued.subject_id, queued.topic_id
        client = get_client(
            "editor",
            settings=self.generator.settings,
            transport=self.generator.transport,
            ledger=LedgerBinding(vault, s, t),
        )
        result = await answer_doubt(
            vault,
            s,
            t,
            request.pending_id,
            _doubt_answer(request.answer),
            client=client,
            sync=sync,
            confirm_over_cap=queued.confirm_over_cap,
            host=self.sessions.host,
            live=None if self.doubts is None else self.doubts.live(s, t),
        )
        reply = result.resolution or "Anotado."
        await broadcast.reply(REPLY_DELTA, {"text": reply, "attempt": 1})
        if self.doubts is not None:
            self.doubts.resolved(s, t, result)
            self.doubts.schedule(s, t)
        return result

    async def _study(self, queued: QueuedRequest, broadcast: TurnBroadcast) -> BaseModel:
        """End the capture and label the study version, as `POST .../study` (under our lock)."""
        s, t = queued.subject_id, queued.topic_id
        state = await switch_to_study(self.sessions, self.hub, s, t, reason="command")
        reply = study_reply(state)
        await broadcast.reply(REPLY_DELTA, {"text": reply, "attempt": 1})
        return StudyTurn(
            turn_id=broadcast.turn_id,
            origin=broadcast.origin,
            request=queued.chat_request(),
            message=queued.request.text,
            reply=reply,
            action=GoStudyAction(path=study_path(s, t)),
            study=state,
        )

    # -- where the events go ---------------------------------------------------------------------

    def _live_session(self, subject_id: str, topic_id: str) -> str | None:
        """The topic's active session on this backend, if it has one."""
        active = self.sessions.active
        if active is not None and (active.subject_id, active.topic_id) == (subject_id, topic_id):
            return active.session_id
        return None

    async def _write_event(
        self,
        vault: Vault,
        subject_id: str,
        topic_id: str,
        kind: str,
        origin: Origin,
        payload: dict[str, Any],
    ) -> str:
        """Write one event in the topic's live session, else in a review session; its session."""
        session_id = self._live_session(subject_id, topic_id)
        if session_id is not None:
            try:
                await self.bus.publish(session_id, kind, origin, payload)
                return session_id
            except BusError:
                logger.info("the session of %s/%s ended meanwhile", subject_id, topic_id)
        return await self._write_review(subject_id, topic_id, [(kind, origin, payload)])

    # -- typed messages --------------------------------------------------------------------------

    async def post_message(
        self,
        subject_id: str,
        topic_id: str,
        text: str,
        classifier: MessageClassifier | None,
    ) -> TypedMessageResult:
        """Classify a message typed in the workspace chat and queue its requests (#327).

        Each request is persisted as an `assistant.request` (origin `user`, `detector: "typed"`)
        in the topic's live session -- the bus brings it back to this consumer -- or in a review
        session (then queued here directly). A message with no request, or a classifier failure,
        becomes one `edit` (a `question` when it asks something) with the raw text, so nothing
        typed is lost.
        """
        vault = await self.sessions.open_vault()
        message_id = f"msg-{uuid.uuid4().hex[:16]}"
        text = text.strip()
        reported: list[ReportedRequest] = []
        classified = True
        if classifier is not None:
            try:
                reported = await classifier.classify(
                    vault,
                    subject_id,
                    topic_id,
                    text,
                    session_id=self._live_session(subject_id, topic_id),
                )
            except Exception as error:  # a ClassificationError, or anything else: keep the text
                logger.warning(
                    "the typed message of %s/%s was not classified: %s", subject_id, topic_id, error
                )
                classified = False
        if not reported:
            reported = [_fallback(text)]
        async with self._typed_lock:
            session_id = self._live_session(subject_id, topic_id)
            if session_id is not None:
                requests = await self._typed_live(session_id, message_id, text, reported)
                if requests is not None:
                    return TypedMessageResult(
                        message_id=message_id, requests=requests, classified=classified
                    )
            requests = [
                _typed_request(n, message_id, text, request)
                for n, request in enumerate(reported, start=1)
            ]
            # Outstanding until each has its `turn.finished`, so a restart runs them (#423).
            outstanding = {"request_ids": [r.request_id for r in requests]}
            review = await asyncio.to_thread(
                _review_session,
                vault,
                subject_id,
                topic_id,
                self.sessions.host,
                [
                    *((ASSISTANT_REQUEST_KIND, TYPED_ORIGIN, r.payload()) for r in requests),
                    (REQUESTS_OUTSTANDING_KIND, "editor", outstanding),
                ],
            )
            if self.sessions.sync is not None:
                self.sessions.sync.note_change()
        for request in requests:
            self.submit(subject_id, topic_id, review, request)
        return TypedMessageResult(message_id=message_id, requests=requests, classified=classified)

    async def _typed_live(
        self, session_id: str, message_id: str, text: str, reported: list[ReportedRequest]
    ) -> list[AssistantRequest] | None:
        """Publish the requests in the live session (under `_typed_lock`); None when it ended."""
        if session_id not in self._typed_counts:
            session = self.bus.attached(session_id)
            events = [] if session is None else await asyncio.to_thread(session.read_events)
            self._typed_counts[session_id] = sum(
                1
                for event in events
                if event.kind == ASSISTANT_REQUEST_KIND
                and str(event.payload.get("request_id", "")).startswith(TYPED_REQUEST_PREFIX)
            )
        requests: list[AssistantRequest] = []
        for request in reported:
            number = self._typed_counts[session_id] + 1
            typed = _typed_request(number, message_id, text, request)
            try:
                await self.bus.publish(
                    session_id, ASSISTANT_REQUEST_KIND, TYPED_ORIGIN, typed.payload()
                )
            except BusError:
                if requests:  # pragma: no cover - the session ended between two requests
                    return requests
                return None
            self._typed_counts[session_id] = number
            requests.append(typed)
        return requests


class _UnknownSourceError(LookupError):
    """A set-aside or restore target that is not a source of the topic."""


HANDLERS: Mapping[str, Handler] = {
    "edit": AssistantRequestConsumer._revise,
    "question": AssistantRequestConsumer._revise,
    "prepare_notes": AssistantRequestConsumer._prepare,
    "incorporate": AssistantRequestConsumer._incorporate,
    "set_aside": AssistantRequestConsumer._set_aside,
    "restore": AssistantRequestConsumer._restore,
    "doubt_answer": AssistantRequestConsumer._doubt_answer,
    "study": AssistantRequestConsumer._study,
}
"""What runs each request kind."""


def unanswered_requests(
    events: Iterable[Event], session_id: str, turns: Iterable[ChatTurn] = ()
) -> list[AssistantRequest]:
    """The session's `assistant.request`s no turn answered, oldest first (#408).

    Answered: a `turn.finished` of its id in `events`, or a chat turn in `turns` whose transcript
    is that request of `session_id`. Requests of a topic run in order, so the ones before the
    newest answered request all ran (a failure written before `turn.finished` existed too) and
    only later ones can be unanswered. A malformed request is left out.
    """
    answered = {
        turn.transcript.request_id
        for turn in turns
        if turn.transcript is not None and turn.transcript.session_id == session_id
    }
    requests: list[AssistantRequest] = []
    for event in sorted(events, key=lambda event: event.seq):
        if event.kind == TURN_FINISHED_KIND:
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                answered.add(request_id)
        elif event.kind == ASSISTANT_REQUEST_KIND:
            try:
                request = AssistantRequest.model_validate(dict(event.payload))
            except ValidationError:
                continue
            if all(r.request_id != request.request_id for r in requests):
                requests.append(request)
    last = max(
        (n for n, request in enumerate(requests) if request.request_id in answered), default=-1
    )
    return [r for r in requests[last + 1 :] if r.request_id not in answered]


def outstanding_requests(
    vault: Vault, subject_id: str, topic_id: str
) -> list[tuple[str, AssistantRequest]]:
    """The topic's outstanding requests no turn answered, oldest first (#423, blocking):
    `(session_id, request)` for each id a `requests.outstanding` event names with no
    `turn.finished` of that request of that session anywhere in the topic, and no voice chat turn
    whose transcript is it. A named request whose `assistant.request` cannot be found or read is
    left out (logged)."""
    reviews = {meta.id for meta in list_sessions(vault, subject_id, topic_id) if not meta.is_study}
    requests: dict[tuple[str, str], dict[str, Any]] = {}
    finished: set[tuple[str, str]] = set()
    named: list[tuple[str, str]] = []
    for session_id, event in read_topic_events(vault, subject_id, topic_id):
        if event.kind == ASSISTANT_REQUEST_KIND:
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                requests.setdefault((session_id, request_id), dict(event.payload))
        elif event.kind == TURN_FINISHED_KIND:
            request_id = event.payload.get("request_id")
            of = event.payload.get("session_id", session_id)
            if isinstance(request_id, str) and isinstance(of, str):
                finished.add((of, request_id))
        elif event.kind == REQUESTS_OUTSTANDING_KIND and session_id in reviews:
            of = event.payload.get("session_id", session_id)
            ids = event.payload.get("request_ids")
            if isinstance(of, str) and isinstance(ids, list):
                named.extend((of, i) for i in ids if isinstance(i, str))
    pending = [key for key in dict.fromkeys(named) if key not in finished]
    if not pending:
        return []
    finished |= {
        (turn.transcript.session_id, turn.transcript.request_id)
        for turn in chat_history(vault, subject_id, topic_id).turns
        if turn.transcript is not None
    }
    found: list[tuple[str, AssistantRequest]] = []
    for key in pending:
        if key in finished:
            continue
        payload = requests.get(key)
        try:
            if payload is None:
                raise LookupError("no such assistant.request")
            found.append((key[0], AssistantRequest.model_validate(payload)))
        except (LookupError, ValidationError) as error:
            logger.warning(
                "the outstanding request %s of session %s of %s/%s is left out: %s",
                key[1],
                key[0],
                subject_id,
                topic_id,
                error,
            )
    return found


def sources_lookup(sessions: SessionService) -> SourcesLookup:
    """The request detector's context of a topic (#327): `editor.source_status` and the doubt the
    chat is asking now; injected into the observer so it never imports the editor."""

    async def lookup(subject_id: str, topic_id: str) -> RequestContext:
        vault = await sessions.open_vault()
        return await asyncio.to_thread(request_context, vault, subject_id, topic_id)

    return lookup


def request_context(vault: Vault, subject_id: str, topic_id: str) -> RequestContext:
    """The topic's sources with their states and the open doubt asked in the chat (blocking)."""
    sources = [
        RequestSource.model_validate(row.model_dump())
        for row in source_status(vault, subject_id, topic_id)
    ]
    asked = [
        turn for turn in doubt_chat_turns(vault, subject_id, topic_id) if turn.status == "open"
    ]
    doubt = None
    if asked:
        last = asked[-1]
        doubt = AskedDoubtRef(
            pending_id=last.pending_id, question=last.question, suggestions=last.suggestions
        )
    return RequestContext(sources=sources, doubt=doubt)


def _typed_request(
    number: int, message_id: str, text: str, request: ReportedRequest
) -> AssistantRequest:
    fields: dict[str, Any] = {}
    if request.targets:
        fields["targets"] = list(request.targets)
    if request.kind == "doubt_answer":
        fields.update(pending_id=request.pending_id, answer=request.answer)
    return AssistantRequest(
        request_id=f"{TYPED_REQUEST_PREFIX}{number}",
        kind=request.kind,
        summary=request.summary.strip(),
        text=text,
        t_start_ms=0,
        t_end_ms=0,
        detector="typed",
        message_id=message_id,
        **fields,
    )


def _fallback(text: str) -> ReportedRequest:
    """A message no request was found in: an `edit`, or a `question` when it asks something."""
    asks = "?" in text or text.startswith("¿")
    return ReportedRequest(
        kind="question" if asks else "edit", summary=_summary(text), segment_ids=[]
    )


def _summary(text: str) -> str:
    line = " ".join(text.split())
    if len(line) <= SUMMARY_MAX_CHARS:
        return line
    return line[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _doubt_answer(answer: str) -> DoubtAnswer:
    """A suggestion's number (as digits) picks it; anything else is the answer's words."""
    value = answer.strip()
    if value.isdigit() and 1 <= int(value) <= 20:
        try:
            return DoubtAnswer(suggestion=int(value))
        except ValueError:
            pass
    return DoubtAnswer(answer=value)


def _has_transcription(vault: Vault, source_path: str) -> bool:
    path = PurePosixPath(source_path)
    try:
        read_source(vault, path.with_name(path.name.split(".", 1)[0] + ".md").as_posix())
    except (SourceError, OSError):
        return False
    return True


def _target(source_id: str, result: TriageResult | None, *, already: bool) -> TriageTarget:
    """A `set_aside` target with the reasons its triage gives (none for a page triage kept)."""
    reasons: list[TriageReason] = []
    duplicate_of = None
    if result is not None and result.status != "kept":
        reasons = [r for r in result.reasons if r in TRIAGE_REASONS]
        if {"duplicate", "same_content"} & set(reasons):
            duplicate_of = result.duplicate_of
    return TriageTarget(
        source_id=source_id, reasons=reasons, duplicate_of=duplicate_of, already=already
    )


def _reason_text(target: TriageTarget | None) -> str | None:
    """«página en blanco», «repetida de la página 1», ...; None without reasons."""
    if target is None or not target.reasons:
        return None
    texts: list[str] = []
    for code in target.reasons:
        text = REASON_TEXT.get(code, code)
        if code in ("duplicate", "same_content") and target.duplicate_of:
            path = PurePosixPath(target.duplicate_of)
            digits = path.name.removeprefix("page-").split(".", 1)[0]
            if digits.isdigit():
                text = f"{text} de la página {int(digits)}"
        texts.append(text)
    return ", ".join(texts)


def _labels(rows: list[SourceStatus], targets: Mapping[str, TriageTarget] | None = None) -> str:
    """«la página 3 y la página 4»; with `targets`, each with its reason: «la página 9 (página
    en blanco)»."""
    labels = []
    for row in rows:
        reason = _reason_text((targets or {}).get(row.source_id))
        labels.append(row.label if reason is None else f"{row.label} ({reason})")
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " y " + labels[-1]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _triage_reply(
    decision: Literal["set_aside", "restore"],
    done: list[SourceStatus],
    unchanged: list[SourceStatus],
    owed: list[SourceStatus],
    *,
    transcribing: bool,
    targets: Mapping[str, TriageTarget] | None = None,
) -> str:
    """The short chat entry of a set-aside or restore request, Spanish; a target set aside
    with a triage reason says it in brackets («He apartado la página 9 (página en blanco).»)."""
    parts: list[str] = []
    if done and decision == "set_aside":
        parts.append(f"He apartado {_labels(done, targets)}.")
    elif done:
        line = f"He recuperado {_labels(done)}"
        if owed:
            which = "" if len(owed) == len(done) else f" ({_labels(owed)})"
            many = len(owed) > 1
            if transcribing:
                line += f"; {'se están' if many else 'se está'} transcribiendo{which}"
            else:
                line += (
                    f"; {'se transcribirán' if many else 'se transcribirá'}{which} en la"
                    " próxima sesión del tema"
                )
        parts.append(line + ".")
    if unchanged:
        many = len(unchanged) > 1
        state = (
            ("ya estaban apartadas" if many else "ya estaba apartada")
            if decision == "set_aside"
            else ("no estaban apartadas" if many else "no estaba apartada")
        )
        which = _labels(unchanged, targets if decision == "set_aside" else None)
        parts.append(_sentence(f"{which} {state}."))
    return " ".join(parts) or "No había nada que cambiar."


def _review_session(
    vault: Vault,
    subject_id: str,
    topic_id: str,
    host: str,
    events: list[tuple[str, Origin, dict[str, Any]]],
) -> str:
    """Write `events` in a review session started and ended at once for them (blocking)."""
    session = start_session(vault, subject_id, topic_id, host, PROTOCOL_VERSION, kind="review")
    try:
        for kind, origin, payload in events:
            session.append_event(kind, origin, payload)
    finally:
        end_session(session)
    return session.id


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
    if isinstance(error, IncorporationError):
        return 422, str(error), None
    if isinstance(error, _UnknownSourceError):
        return 404, UNKNOWN_SOURCE_DETAIL, None
    if isinstance(error, DoubtError):
        code = (
            ErrorCode.DOUBT_CLOSED
            if isinstance(error, DoubtClosedError)
            else ErrorCode.SESSION_OPEN
            if isinstance(error, OpenSessionError)
            else None
        )
        status = (
            404
            if isinstance(error, UnknownDoubtError)
            else 422
            if isinstance(error, InvalidAnswerError)
            else 409
        )
        return status, str(error), None if code is None else code.value
    if isinstance(error, SourceError):
        return 404, UNKNOWN_SOURCE_DETAIL, None
    if isinstance(error, VersionError):  # `study`: no notes to label
        return 409, str(error), None
    status, detail, error_code = turn_error(error)
    return status, detail, None if error_code is None else error_code.value


__all__ = [
    "HANDLERS",
    "TURN_FINISHED_KIND",
    "REQUESTS_OUTSTANDING_KIND",
    "outstanding_requests",
    "request_context",
    "unanswered_requests",
    "sources_lookup",
    "TURN_KINDS",
    "NOT_STOPPED_DETAIL",
    "UNKNOWN_KIND_DETAIL",
    "AssistantRequestConsumer",
    "NotStoppedError",
    "QueuedRequest",
    "TypedMessageResult",
]
