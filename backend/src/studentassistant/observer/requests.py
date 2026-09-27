"""Requests to the assistant, detected by Sonnet (role `observer`) in the raw transcript (#314),
and the same classification of a message typed in the workspace chat (#327).

`RequestDetector` subscribes to the session bus (`transcript.final` and the lifecycle events) and,
per active session, keeps the newest final segments and which of them it has examined. Once no new
final has arrived for `request_debounce_seconds`, or `request_max_wait_seconds` after the first
final it has not examined yet, it asks Claude whether the window of the newest
`request_window_segments` finals holds a request to the assistant. It runs apart from the batch
`ObserverLoop` (its own calls, its own trigger), so a request is never delayed by the loop's
batches. One call per session is in flight at a time; finals arriving meanwhile coalesce into the
next call.

Each call is self-contained: system = the `observer_requests` prompt + the topic block (subject,
title), the cached prefix together with the tool; one user turn = the topic's context (uncached:
its sources with their states and the doubt asked in the chat now, from the injected
`sources_lookup`, #327), then the window, each final marked `new`, `seen` or with the request it
already belongs to, so no segment is reported twice. The answer must call the strict tool
`report_requests` (`{requests: [{kind, summary, segment_ids, targets, pending_id, answer}]}`).
Each request is checked (a Spanish `summary` of at most 140 characters, `segment_ids` consecutive
in the window and not already assigned, `targets` among the topic's sources -- only captured pages
for `set_aside`/`restore` --, a `doubt_answer` only for the doubt asked now); valid ones are
published at once as persisted `assistant.request` events (origin `observer`, payload
`AssistantRequest`), the rest are re-asked `client.structured_reasks` times (`[llm]
structured_reasks`) and then dropped and logged.

`MessageClassifier` classifies one typed chat message with the same prompt, tool and checks (the
message is the window, as the single segment `m1`); the server persists and queues what it finds.

Cost caps: the client is bound to the session's ledger. A reached cap pauses the detector: the
finals stay unexamined, one `observer.status` event (`status: paused`, `detector: "requests"`) is
published and the next final tries again; `status: running` follows a successful call. Any other
Claude failure does the same with `status: error` plus an `observer.call_failed` notice.

The conversation file is `conversations/observer-requests-<session-id>.jsonl` (a `context` record
when a session is first seen, then each `user` turn and `assistant` answer, and `status` changes).
`flush(session_id)` is the `add_before_ended` hook: it sends what is still unexamined at once and
waits for it, so a request spoken just before ending is still detected.

Wake-word mode (`request_detection = "wake_word"`, #318): no Claude call at all. The detector reads
`voice.command` events instead, and each `assistant_request` command ("anel, haz una tabla...", the
wake word lives only in the stt grammar) becomes a persisted `assistant.request` at once (origin
`stt`, `detector: "wake_word"`): `kind` `prepare_notes` when the query says "prepárame el tema",
else `edit`; `text` = the query, `summary` = the query cut to 140 characters at a word boundary,
`segment_ids` = the command's segment, times from that segment's `transcript.final`. A request is
one final: what follows a pause after the wake word is another segment and is not part of it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from studentassistant.config import ObserverSettings
from studentassistant.llm import (
    CostCapReachedError,
    LedgerBinding,
    LLMClient,
    LLMError,
    LLMResponse,
    load_prompt,
    strict_tool,
)
from studentassistant.observer.assistant_request import (
    ANSWER_MAX_CHARS,
    ASSISTANT_REQUEST_KIND,
    CAPTURE_KINDS,
    SUMMARY_MAX_CHARS,
    TARGET_KINDS,
    AssistantRequest,
    RequestContext,
    RequestKind,
    SourcesLookup,
)
from studentassistant.observer.context import OBSERVER_ORIGIN, render_topic
from studentassistant.observer.fold import SEGMENT_EVENT_KIND, SEGMENT_ID_KEY
from studentassistant.observer.live import (
    CALL_FAILED_EVENT_KIND,
    SESSION_ENDED,
    SESSION_RESUMED,
    SESSION_STARTED,
    STATUS_EVENT_KIND,
    BusEventLike,
    CallFailureKind,
    ClientFactory,
    EventBus,
    SessionLookup,
    Status,
    SubscriptionLike,
    _failure_kind,
    default_client_factory,
)
from studentassistant.stt.commands import ASSISTANT_REQUEST, VOICE_COMMAND, normalise
from studentassistant.vault import (
    ConversationRecord,
    SecretRefused,
    Session,
    VaultError,
    append_conversation_record,
    get_subject,
    get_topic,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "report_requests"
TOOL_DESCRIPTION = (
    "Report the requests to the assistant found in the window of the transcript. Call it exactly "
    "once per window, with every request not reported yet (an empty list when there is none)."
)
PROMPT_NAME = "observer_requests"
DETECTOR = "requests"
"""The `detector` field of the detector's `observer.status` events."""
REQUEST_KINDS_READ = frozenset(
    {SEGMENT_EVENT_KIND, SESSION_STARTED, SESSION_RESUMED, SESSION_ENDED}
)
WAKE_WORD_KINDS_READ = frozenset({VOICE_COMMAND, SEGMENT_EVENT_KIND, SESSION_ENDED})
"""What the detector reads in wake-word mode: the commands, and the finals for their times."""
WAKE_WORD_ORIGIN = "stt"
PREPARE_NOTES_PHRASE = normalise("prepárame el tema")
"""A wake-word query containing it asks for `prepare_notes` (the grammar's "prepárame el tema")."""
SEGMENT_TIMES_KEPT = 64
"""The newest finals whose times a wake-word session keeps (its command follows at once)."""
QUEUE_SIZE = 1024


MESSAGE_SEGMENT = "m1"
"""The one segment id a typed message is shown as."""
MESSAGES_CONVERSATION = "observer-messages"
"""The conversation file of the typed messages' classifications (`conversations/<name>.jsonl`)."""


class ReportedRequest(BaseModel):
    """One request of the `report_requests` tool input."""

    kind: RequestKind
    summary: str
    segment_ids: list[str]
    targets: list[str] = Field(
        default_factory=list,
        description="incorporate, set_aside, restore: the source ids, from the sources list.",
    )
    pending_id: str | None = Field(
        default=None, description="doubt_answer: the id of the doubt asked now."
    )
    answer: str | None = Field(
        default=None,
        description="doubt_answer: the suggestion's number as digits, or the answer's words.",
    )


class ReportRequests(BaseModel):
    """The input of the `report_requests` tool."""

    requests: list[ReportedRequest]


def requests_tool() -> dict[str, Any]:
    """The strict `report_requests` tool."""
    return strict_tool(TOOL_NAME, TOOL_DESCRIPTION, ReportRequests)


# -- time --------------------------------------------------------------------------------------


class Clock(Protocol):
    """Where the detector reads the time: `now` stamps records, `monotonic`/`sleep` the triggers."""

    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# -- one session -------------------------------------------------------------------------------


@dataclass
class _Final:
    segment_id: str
    text: str
    start_ms: int
    end_ms: int
    arrived: float  # the clock's `monotonic()` when it was received
    examined: bool = False


@dataclass
class _Checked:
    """The valid requests of one answer, and why the rest were refused."""

    requests: list[tuple[ReportedRequest, list[_Final]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    called: bool = False


@dataclass
class _Detected:
    session: Session
    client: LLMClient
    system: list[str]
    finals: list[_Final] = field(default_factory=list)
    # segment id -> the request it belongs to (earlier sessions' requests of a resumed one too).
    assigned: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    calls: int = 0
    last_final_at: float | None = None
    timer: asyncio.Task[None] | None = None
    call: asyncio.Task[None] | None = None
    status: Status = "running"
    # After a failed call the unexamined finals wait for a new one, so a failure is not a loop.
    blocked: bool = False

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def conversation_name(self) -> str:
        return f"observer-requests-{self.session.id}"

    def unexamined(self) -> list[_Final]:
        return [final for final in self.finals if not final.examined]


@dataclass
class _Spoken:
    """A session in wake-word mode: its request count, used segments and newest finals' times."""

    requests: int = 0
    assigned: set[str] = field(default_factory=set)
    # segment id -> (session_start_ms, session_end_ms), newest last.
    times: dict[str, tuple[int, int]] = field(default_factory=dict)


class RequestDetector:
    """The app-wide request detector: `start()` subscribes, `stop()` ends it, `flush()` a session.

    With `settings.request_detection == "observer"` it asks Sonnet; with `"wake_word"` it turns
    the `assistant_request` voice commands into requests without any call; with `"off"` `start()`
    does nothing. `lookup` gives an attached session's vault handle; `client_factory`
    builds a session's `observer` client from its ledger binding (tests pass
    `default_client_factory(transport=fake)`); `clock` gives the time (`SystemClock`);
    `sources_lookup` the topic's context of each call (none: no sources, no doubt asked).
    """

    def __init__(
        self,
        bus: EventBus,
        lookup: SessionLookup,
        *,
        settings: ObserverSettings | None = None,
        client_factory: ClientFactory | None = None,
        clock: Clock | None = None,
        queue_size: int = QUEUE_SIZE,
        sources_lookup: SourcesLookup | None = None,
    ) -> None:
        self.bus = bus
        self.lookup = lookup
        self.sources_lookup = sources_lookup
        self.settings = settings or ObserverSettings()
        self.client_factory = client_factory or default_client_factory()
        self.clock: Clock = clock or SystemClock()
        self.queue_size = queue_size
        self.prompt = load_prompt(PROMPT_NAME)
        self.tool = requests_tool()
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._detected: dict[str, _Detected] = {}
        self._spoken: dict[str, _Spoken] = {}
        self._ignored: set[str] = set()
        self._busy = False
        self._settled = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.request_detection != "off"

    @property
    def wake_word(self) -> bool:
        return self.settings.request_detection == "wake_word"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Subscribe to the bus and start consuming (inside the running loop); off: nothing."""
        if self._subscription is not None:
            return
        if not self.enabled:
            logger.info(
                "request detection is %r: the observer detects no requests",
                self.settings.request_detection,
            )
            return
        kinds = WAKE_WORD_KINDS_READ if self.wake_word else REQUEST_KINDS_READ
        self._subscription = self.bus.subscribe(
            name="observer-requests", kinds=kinds, maxsize=self.queue_size
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="observer-requests")

    async def stop(self) -> None:
        """Stop receiving, wait for the calls in flight, end the task."""
        subscription, task = self._subscription, self._task
        if subscription is None or task is None:
            return
        subscription.close()
        try:
            await task
        finally:
            self._subscription = None
            self._task = None
            self._settled.set()
        for detected in self._detected.values():
            self._cancel_timer(detected)
        calls = [d.call for d in self._detected.values() if d.call is not None]
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        self._detected.clear()
        self._spoken.clear()

    async def drain(self) -> None:
        """Return once every event delivered to the detector so far has been consumed."""
        await asyncio.sleep(0)
        while (
            self.running
            and self._subscription is not None
            and (len(self._subscription) or self._busy)
        ):
            self._settled.clear()
            await self._settled.wait()

    async def wait_idle(self, session_id: str) -> None:
        """Return once `session_id` has no call in flight (awaited shielded, never cancelled)."""
        await self.drain()
        while True:
            detected = self._detected.get(session_id)
            if detected is None or detected.call is None:
                return
            await asyncio.shield(detected.call)
            await self.drain()

    async def flush(self, session_id: str) -> None:
        """Examine what `session_id` still has unexamined, at once (the before-ended hook)."""
        await self.wait_idle(session_id)
        detected = self._detected.get(session_id)
        if detected is not None and detected.call is None and detected.unexamined():
            self._cancel_timer(detected)
            self._start_call(detected, last=True)
            await self.wait_idle(session_id)

    def status(self, session_id: str) -> Status | None:
        """`running`, `paused` (a cost cap) or `error` for a watched session, else `None`."""
        detected = self._detected.get(session_id)
        return None if detected is None else detected.status

    # -- consumer ----------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            self._busy = True
            try:
                await self._on_event(event)
            except Exception:
                logger.exception(
                    "the request detector failed on a %s event of session %s",
                    event.kind,
                    event.session_id,
                )
            finally:
                self._busy = False
                if not len(subscription):
                    self._settled.set()

    async def _on_event(self, event: BusEventLike) -> None:
        if self.wake_word:
            await self._on_wake_word_event(event)
            return
        session_id = event.session_id
        if event.kind == SESSION_ENDED:
            detected = self._detected.pop(session_id, None)
            if detected is not None:
                self._cancel_timer(detected)
            self._ignored.discard(session_id)
            return
        if session_id in self._ignored:
            return
        detected = self._detected.get(session_id)
        if detected is None:
            detected = await self._open(session_id, resumed=event.kind == SESSION_RESUMED)
            if detected is None:
                return
        if event.kind == SEGMENT_EVENT_KIND:
            self._add_final(detected, event)

    async def _open(self, session_id: str, *, resumed: bool) -> _Detected | None:
        session = self.lookup(session_id)
        if session is None:
            logger.warning("no open vault session %s for the request detector", session_id)
            self._ignored.add(session_id)
            return None
        vault, subject, topic = session.vault, session.subject_slug, session.topic_slug
        try:
            subject_name, topic_title, earlier = await asyncio.to_thread(
                self._read_session, session
            )
        except (VaultError, OSError):
            logger.exception("the request detector cannot read session %s", session_id)
            self._ignored.add(session_id)
            return None
        detected = _Detected(
            session=session,
            client=self.client_factory(LedgerBinding(vault, subject, topic, session_id)),
            system=[self.prompt.content, render_topic(subject_name, topic_title, None)],
            requests=max((_spoken_number(r.request_id) for r in earlier), default=0),
        )
        for request in earlier:
            for segment_id in request.segment_ids:
                detected.assigned[segment_id] = request.request_id
        self._detected[session_id] = detected
        await self._record(
            detected,
            ConversationRecord(
                time=self.clock.now(),
                kind="context",
                model=detected.client.model,
                prompt_hash=self.prompt.hash,
                detail={
                    "reason": "resume" if resumed else "start",
                    "requests": len(earlier),
                    "window_segments": self.settings.request_window_segments,
                },
            ),
        )
        return detected

    @staticmethod
    def _read_session(session: Session) -> tuple[str, str, list[AssistantRequest]]:
        vault, subject, topic = session.vault, session.subject_slug, session.topic_slug
        earlier = _earlier_requests(session)
        subject_name = get_subject(vault, subject).subject.name
        topic_title = get_topic(vault, subject, topic).topic.title
        return subject_name, topic_title, earlier

    def _add_final(self, detected: _Detected, event: BusEventLike) -> None:
        payload = event.payload
        segment_id = payload.get(SEGMENT_ID_KEY)
        text = payload.get("text")
        if not isinstance(segment_id, str) or not segment_id or not isinstance(text, str):
            return
        if not text.strip() or any(f.segment_id == segment_id for f in detected.finals):
            return
        start = _ms(payload.get("session_start_ms"), event.t)
        end = max(start, _ms(payload.get("session_end_ms"), start))
        now = self.clock.monotonic()
        detected.finals.append(_Final(segment_id, text.strip(), start, end, now))
        self._trim(detected)
        detected.last_final_at = now
        detected.blocked = False
        self._maybe_start(detected)

    def _trim(self, detected: _Detected) -> None:
        """Keep only the window (a final that leaves it unexamined is never examined)."""
        excess = len(detected.finals) - self.settings.request_window_segments
        if excess <= 0:
            return
        dropped = detected.finals[:excess]
        del detected.finals[:excess]
        lost = [final.segment_id for final in dropped if not final.examined]
        if lost:
            logger.warning(
                "request detector of session %s: %d finals left the window unexamined: %s",
                detected.id,
                len(lost),
                ", ".join(lost),
            )

    # -- wake word ---------------------------------------------------------------------------

    async def _on_wake_word_event(self, event: BusEventLike) -> None:
        session_id = event.session_id
        if event.kind == SESSION_ENDED:
            self._spoken.pop(session_id, None)
            self._ignored.discard(session_id)
            return
        if session_id in self._ignored:
            return
        spoken = self._spoken.get(session_id)
        if spoken is None:
            spoken = await self._open_spoken(session_id)
            if spoken is None:
                return
        payload = event.payload
        if event.kind == SEGMENT_EVENT_KIND:
            segment_id = payload.get(SEGMENT_ID_KEY)
            if isinstance(segment_id, str) and segment_id:
                start = _ms(payload.get("session_start_ms"), event.t)
                spoken.times.pop(segment_id, None)
                spoken.times[segment_id] = (
                    start,
                    max(start, _ms(payload.get("session_end_ms"), start)),
                )
                while len(spoken.times) > SEGMENT_TIMES_KEPT:
                    del spoken.times[next(iter(spoken.times))]
            return
        if event.kind == VOICE_COMMAND and payload.get("command") == ASSISTANT_REQUEST:
            await self._publish_wake_word(session_id, spoken, event)

    async def _open_spoken(self, session_id: str) -> _Spoken | None:
        session = self.lookup(session_id)
        if session is None:
            logger.warning("no open vault session %s for the wake-word requests", session_id)
            self._ignored.add(session_id)
            return None
        try:
            earlier = await asyncio.to_thread(_earlier_requests, session)
        except (VaultError, OSError):
            logger.exception("the wake-word requests cannot read session %s", session_id)
            self._ignored.add(session_id)
            return None
        spoken = _Spoken(
            requests=max((_spoken_number(r.request_id) for r in earlier), default=0),
            assigned={segment_id for r in earlier for segment_id in r.segment_ids},
        )
        self._spoken[session_id] = spoken
        return spoken

    async def _publish_wake_word(
        self, session_id: str, spoken: _Spoken, event: BusEventLike
    ) -> None:
        payload = event.payload
        segment_id, query = payload.get("segment_id"), payload.get("query")
        if not isinstance(segment_id, str) or not segment_id or not isinstance(query, str):
            logger.warning(
                "session %s: a malformed %s command; skipped", session_id, ASSISTANT_REQUEST
            )
            return
        query = query.strip()
        if not normalise(query) or segment_id in spoken.assigned:
            return
        start, end = spoken.times.get(segment_id) or (_ms(event.t, 0), _ms(event.t, 0))
        number = spoken.requests + 1
        request = AssistantRequest(
            request_id=f"req-{number}",
            kind=_wake_word_kind(query),
            summary=_cut_summary(query),
            text=query,
            segment_ids=[segment_id],
            t_start_ms=start,
            t_end_ms=end,
            detector="wake_word",
        )
        try:
            await self.bus.publish(
                session_id, ASSISTANT_REQUEST_KIND, WAKE_WORD_ORIGIN, request.payload()
            )
        except Exception as error:  # the session ended meanwhile, a refused secret...
            logger.warning("session %s: a wake-word request not published: %s", session_id, error)
            return
        spoken.requests = number
        spoken.assigned.add(segment_id)
        logger.info(
            "wake-word request %s (%s) in segment %s of session %s",
            request.request_id,
            request.kind,
            segment_id,
            session_id,
        )

    # -- triggers ----------------------------------------------------------------------------

    def _deadline(self, detected: _Detected) -> float | None:
        unexamined = detected.unexamined()
        if detected.last_final_at is None or not unexamined:
            return None
        return min(
            detected.last_final_at + self.settings.request_debounce_seconds,
            unexamined[0].arrived + self.settings.request_max_wait_seconds,
        )

    def _maybe_start(self, detected: _Detected) -> None:
        if detected.call is not None or detected.blocked or not detected.unexamined():
            return
        deadline = self._deadline(detected)
        if deadline is None:
            return
        if self.clock.monotonic() >= deadline:
            self._cancel_timer(detected)
            self._start_call(detected)
        elif detected.timer is None:
            detected.timer = asyncio.create_task(
                self._wait(detected), name=f"observer-requests-timer:{detected.id}"
            )

    async def _wait(self, detected: _Detected) -> None:
        """Sleep until the trigger's deadline (moved by each new final), then try to start."""
        try:
            while True:
                deadline = self._deadline(detected)
                if deadline is None:
                    return
                remaining = deadline - self.clock.monotonic()
                if remaining <= 0:
                    break
                await self.clock.sleep(remaining)
        finally:
            if detected.timer is asyncio.current_task():
                detected.timer = None
        if self._detected.get(detected.id) is detected:
            self._maybe_start(detected)

    @staticmethod
    def _cancel_timer(detected: _Detected) -> None:
        timer, detected.timer = detected.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    def _start_call(self, detected: _Detected, *, last: bool = False) -> None:
        detected.call = asyncio.create_task(
            self._call(detected, last=last), name=f"observer-requests:{detected.id}"
        )

    async def _call(self, detected: _Detected, *, last: bool) -> None:
        try:
            await self._examine(detected, last=last)
        except Exception as error:
            logger.exception("the request detection of session %s failed", detected.id)
            await self._report_failure(detected, "error", repr(error))
            detected.blocked = True
        finally:
            detected.call = None
            if self._detected.get(detected.id) is detected:
                self._maybe_start(detected)

    # -- one call ----------------------------------------------------------------------------

    async def _examine(self, detected: _Detected, *, last: bool) -> None:
        window = list(detected.finals)
        examining = [final for final in window if not final.examined]
        if not examining:
            return
        detected.calls += 1
        context = await _context_of(
            self.sources_lookup, detected.session.subject_slug, detected.session.topic_slug
        )
        text = context.render() + "\n\n" + self._render_window(detected, window, last)
        turn = {"role": "user", "content": [{"type": "text", "text": text}]}
        response = await self._ask(detected, [turn])
        if response is None:
            detected.blocked = True
            return
        # Examined once answered, whatever the answer: a final is never examined twice as new.
        for final in examining:
            final.examined = True
        position = {final.segment_id: index for index, final in enumerate(window)}
        checked = _check(response, position, set(detected.assigned), context, window)
        published = await self._publish(detected, checked)
        messages: list[dict[str, Any]] = [turn]
        reasks = detected.client.structured_reasks
        for _ in range(reasks):
            if not checked.errors and checked.called:
                break
            messages = [*messages, response.assistant_turn(), _reask_turn(response, checked)]
            retry = await self._ask(detected, messages)
            if retry is None:
                return
            response = retry
            checked = _check(response, position, set(detected.assigned), context, window)
            published += await self._publish(detected, checked)
        if checked.errors or not checked.called:
            logger.warning(
                "request detector of session %s: dropped after %s: %s",
                detected.id,
                "one re-ask" if reasks == 1 else f"{reasks} re-asks",
                "; ".join(checked.errors or [f"no {TOOL_NAME} call"]),
            )
        logger.info(
            "request detector of session %s: call %d, %d new finals, %d requests",
            detected.id,
            detected.calls,
            len(examining),
            published,
        )

    def _render_window(self, detected: _Detected, window: list[_Final], last: bool) -> str:
        new = sum(1 for final in window if not final.examined)
        lines = [
            f"Window {detected.calls}: the newest {len(window)} final segments, {new} new.",
        ]
        for final in window:
            mark = detected.assigned.get(final.segment_id) or ("seen" if final.examined else "new")
            lines.append(
                f"{final.segment_id} [{final.start_ms / 1000:.1f}s-{final.end_ms / 1000:.1f}s]"
                f" {mark} {final.text}"
            )
        if last:
            lines.append(
                "The session is ending: nothing more will follow, so report a request even if it"
                " seems cut off."
            )
        return "\n".join(lines)

    async def _ask(self, detected: _Detected, messages: list[dict[str, Any]]) -> LLMResponse | None:
        """One call; `None` when it failed (a reached cap pauses, any other error is reported)."""
        try:
            response = await detected.client.create(
                messages,
                system=detected.system,
                tools=[self.tool],
                tool_choice={"type": "auto"},
                prompt_hash=self.prompt.hash,
            )
        except CostCapReachedError as error:
            await self._set_status(
                detected,
                "paused",
                str(error),
                {"cap": error.cap, "limit_usd": error.limit_usd, "total_usd": error.total_usd},
            )
            return None
        except LLMError as error:
            logger.warning("request detection of session %s failed: %s", detected.id, error)
            await self._report_failure(detected, _failure_kind(error), str(error))
            await self._set_status(detected, "error", str(error), {})
            return None
        await self._record(
            detected, ConversationRecord(time=self.clock.now(), kind="user", message=messages[-1])
        )
        await self._record(
            detected,
            ConversationRecord(
                time=self.clock.now(),
                kind="assistant",
                message=response.assistant_turn(),
                model=response.model,
                prompt_hash=self.prompt.hash,
                usage=response.usage.model_dump(),
            ),
        )
        if detected.status != "running":
            await self._set_status(detected, "running", "", {})
        return response

    async def _publish(self, detected: _Detected, checked: _Checked) -> int:
        count = 0
        for request, span in checked.requests:
            number = detected.requests + 1
            payload = AssistantRequest(
                request_id=f"req-{number}",
                kind=request.kind,
                summary=request.summary.strip(),
                text=" ".join(final.text for final in span),
                segment_ids=[final.segment_id for final in span],
                t_start_ms=span[0].start_ms,
                t_end_ms=max(span[0].start_ms, span[-1].end_ms),
                detector="observer",
                **_kind_fields(request),
            )
            try:
                await self.bus.publish(
                    detected.id, ASSISTANT_REQUEST_KIND, OBSERVER_ORIGIN, payload.payload()
                )
            except Exception as error:  # the session ended under the call, a refused secret...
                logger.warning(
                    "request detector of session %s could not publish a request: %s",
                    detected.id,
                    error,
                )
                continue
            detected.requests = number
            for segment_id in payload.segment_ids:
                detected.assigned[segment_id] = payload.request_id
            count += 1
        return count

    async def _report_failure(
        self, detected: _Detected, kind: CallFailureKind, reason: str
    ) -> None:
        try:
            await self.bus.publish(
                detected.id,
                CALL_FAILED_EVENT_KIND,
                OBSERVER_ORIGIN,
                {"kind": kind, "reason": reason, "detector": DETECTOR},
                persist=False,
            )
        except Exception as error:  # the session ended meanwhile
            logger.warning(
                "request detector of session %s: failure not published: %s", detected.id, error
            )

    async def _set_status(
        self, detected: _Detected, status: Status, reason: str, detail: Mapping[str, Any]
    ) -> None:
        if detected.status == status:
            return
        detected.status = status
        try:
            await self.bus.publish(
                detected.id,
                STATUS_EVENT_KIND,
                OBSERVER_ORIGIN,
                {"status": status, "reason": reason, **detail, "detector": DETECTOR},
            )
        except Exception as error:
            logger.warning(
                "request detector of session %s: status %s not published: %s",
                detected.id,
                status,
                error,
            )
        await self._record(
            detected,
            ConversationRecord(
                time=self.clock.now(),
                kind="status",
                detail={"status": status, "reason": reason, "detector": DETECTOR},
            ),
        )

    async def _record(self, detected: _Detected, record: ConversationRecord) -> None:
        session = detected.session
        try:
            await asyncio.to_thread(
                append_conversation_record,
                session.vault,
                session.subject_slug,
                session.topic_slug,
                detected.conversation_name,
                record,
            )
        except SecretRefused:
            logger.warning(
                "a request detector record of session %s looks like a secret; not written",
                detected.id,
            )
        except (VaultError, OSError):
            logger.exception(
                "the request detector conversation of session %s cannot be written", detected.id
            )


def _check(
    response: LLMResponse,
    position: Mapping[str, int],
    taken: set[str],
    context: RequestContext,
    window: Sequence[_Final],
    *,
    typed: bool = False,
) -> _Checked:
    """The valid requests of `response`, in order; errors for the rest.

    `position` maps the window's segment ids to their index, `taken` holds the segments already
    part of a request. A typed message (`typed`) is one segment that every request of it shares.
    """
    result = _Checked()
    if response.stop_reason == "refusal":
        result.called = True
        result.errors.append("the request was declined")
        return result
    taken = set(taken)
    for call in response.tool_calls:
        if call.name != TOOL_NAME:
            result.errors.append(f"unknown tool `{call.name}`")
            continue
        result.called = True
        try:
            data = call.parsed_input()
        except ValueError as error:
            result.errors.append(f"the tool input is not valid JSON: {error}")
            continue
        raw_requests = data.get("requests") if isinstance(data, dict) else None
        if not isinstance(raw_requests, list):
            result.errors.append("the tool input has no `requests` list")
            continue
        for index, raw in enumerate(raw_requests):
            try:
                request = ReportedRequest.model_validate(raw)
            except ValidationError as error:
                detail = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
                )
                result.errors.append(f"request {index} is malformed ({detail})")
                continue
            if typed:
                request = request.model_copy(update={"segment_ids": [MESSAGE_SEGMENT]})
            problem = _summary_problem(request) or (
                None if typed else _span_problem(request, position, taken)
            )
            if problem is None:
                request, problem = _kind_problem(request, context)
            if problem:
                result.errors.append(f"request {index}: {problem}")
                continue
            if not typed:
                taken.update(request.segment_ids)
            span = [window[position[segment_id]] for segment_id in request.segment_ids]
            result.requests.append((request, span))
    return result


def _reask_turn(response: LLMResponse, checked: _Checked) -> dict[str, Any]:
    """The user turn answering an answer with refused requests (or no tool call)."""
    reasons = checked.errors or [f"you did not call the `{TOOL_NAME}` tool"]
    content: list[dict[str, Any]] = [
        {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": "Errors:\n" + "\n".join(reasons),
            "is_error": True,
        }
        for call in response.tool_calls
    ]
    content.append(
        {
            "type": "text",
            "text": (
                f"{len(checked.requests)} requests were accepted. These were refused:\n"
                + "\n".join(f"- {reason}" for reason in reasons)
                + f"\nCall `{TOOL_NAME}` again with only the corrected requests (or an"
                " empty list)."
            ),
        }
    )
    return {"role": "user", "content": content}


def _summary_problem(request: ReportedRequest) -> str | None:
    summary = request.summary.strip()
    if not summary:
        return "the summary is empty"
    if len(summary) > SUMMARY_MAX_CHARS:
        return f"the summary has {len(summary)} characters; at most {SUMMARY_MAX_CHARS}"
    return None


def _span_problem(
    request: ReportedRequest, position: Mapping[str, int], taken: set[str]
) -> str | None:
    """Why a reported request's span is refused, or `None` when it is valid."""
    ids = request.segment_ids
    if not ids:
        return "segment_ids is empty"
    unknown = [segment_id for segment_id in ids if segment_id not in position]
    if unknown:
        return f"segments not in the window: {', '.join(unknown)}"
    if len(set(ids)) != len(ids):
        return "segment_ids repeats a segment"
    indexes = [position[segment_id] for segment_id in ids]
    if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        return "segment_ids must be consecutive segments of the window, in order"
    reported = [segment_id for segment_id in ids if segment_id in taken]
    if reported:
        return f"segments already part of a request: {', '.join(reported)}"
    return None


def _kind_problem(
    request: ReportedRequest, context: RequestContext
) -> tuple[ReportedRequest, str | None]:
    """The request with only its kind's fields, and why its targets or answer are refused."""
    kind = request.kind
    if kind in TARGET_KINDS:
        targets = list(dict.fromkeys(t.strip() for t in request.targets if t.strip()))
        cleaned = request.model_copy(
            update={"targets": targets, "pending_id": None, "answer": None}
        )
        if not targets:
            return cleaned, f"a {kind} request needs its targets (source ids of the list)"
        unknown = [t for t in targets if context.source(t) is None]
        if unknown:
            return cleaned, (
                f"targets not in the sources list: {', '.join(unknown)} (an unclear reference"
                " is a `question`)"
            )
        if kind in ("set_aside", "restore"):
            other = [t for t in targets if context.source(t).kind not in CAPTURE_KINDS]  # type: ignore[union-attr]
            if other:
                return cleaned, (
                    f"only captured pages (apuntes, libro) can be set aside or restored:"
                    f" {', '.join(other)}"
                )
        return cleaned, None
    if kind == "doubt_answer":
        doubt = context.doubt
        answer = (request.answer or "").strip()
        pending_id = (request.pending_id or "").strip() or (doubt.pending_id if doubt else "")
        cleaned = request.model_copy(
            update={"targets": [], "pending_id": pending_id or None, "answer": answer or None}
        )
        if doubt is None:
            return cleaned, "no doubt is asked in the chat now, so nothing is a doubt_answer"
        if pending_id != doubt.pending_id:
            return cleaned, f"the doubt asked now is {doubt.pending_id}, not {pending_id}"
        if not answer:
            return cleaned, "a doubt_answer needs the student's answer"
        if len(answer) > ANSWER_MAX_CHARS:
            return cleaned, f"the answer has {len(answer)} characters; at most {ANSWER_MAX_CHARS}"
        return cleaned, None
    return request.model_copy(update={"targets": [], "pending_id": None, "answer": None}), None


def _kind_fields(request: ReportedRequest) -> dict[str, Any]:
    """The `AssistantRequest` fields of a checked request's kind."""
    if request.kind in TARGET_KINDS:
        return {"targets": list(request.targets)}
    if request.kind == "doubt_answer":
        return {"pending_id": request.pending_id, "answer": request.answer}
    return {}


async def _context_of(
    lookup: SourcesLookup | None, subject_slug: str, topic_slug: str
) -> RequestContext:
    """The topic's context for a call; empty without a lookup or when it fails."""
    if lookup is None:
        return RequestContext()
    try:
        return await lookup(subject_slug, topic_slug)
    except Exception:
        logger.exception("the request context of %s/%s cannot be read", subject_slug, topic_slug)
        return RequestContext()


def _earlier_requests(session: Session) -> list[AssistantRequest]:
    """The session's `assistant.request` events so far (earlier sessions' of a resumed one)."""
    earlier: list[AssistantRequest] = []
    for event in session.read_events():
        if event.kind != ASSISTANT_REQUEST_KIND:
            continue
        try:
            earlier.append(AssistantRequest.model_validate(event.payload))
        except ValidationError:
            logger.warning("session %s: an unreadable %s event", session.id, event.kind)
    return earlier


def _wake_word_kind(query: str) -> RequestKind:
    """`prepare_notes` when the query says "prepárame el tema", else `edit`."""
    words = f" {normalise(query)} "
    return "prepare_notes" if f" {PREPARE_NOTES_PHRASE} " in words else "edit"


def _cut_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """`text` on one line, cut to at most `limit` characters at a word boundary."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    head = line[: limit + 1]
    cut = head.rsplit(" ", 1)[0].rstrip(" ,.;:-") if " " in head else ""
    return cut or line[:limit]


def _spoken_number(request_id: str) -> int:
    """`n` of a spoken `req-<n>`; 0 for a typed `req-t<n>`."""
    number = request_id.removeprefix("req-")
    return int(number) if number.isdigit() else 0


# -- typed messages ------------------------------------------------------------------------------


class ClassificationError(Exception):
    """A typed message could not be classified (Claude failed, or no valid answer)."""


class MessageClassifier:
    """Classifies one message typed in the workspace chat into requests (#327).

    The same prompt, tool and checks as `RequestDetector`, the message as the window's single
    segment `m1` (every request of it shares that segment), the topic's context from
    `sources_lookup`. `client_factory` builds the `observer` client from a ledger binding (the
    topic's live session when it has one). The calls are recorded in
    `conversations/observer-messages.jsonl` (`user` and `assistant` records).
    """

    def __init__(
        self,
        client_factory: ClientFactory | None = None,
        *,
        sources_lookup: SourcesLookup | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.client_factory = client_factory or default_client_factory()
        self.sources_lookup = sources_lookup
        self.clock: Clock = clock or SystemClock()
        self.prompt = load_prompt(PROMPT_NAME)
        self.tool = requests_tool()

    async def classify(
        self,
        vault: Any,
        subject_slug: str,
        topic_slug: str,
        text: str,
        *,
        session_id: str | None = None,
    ) -> list[ReportedRequest]:
        """The requests of `text`, checked (possibly none).

        Raises:
            ClassificationError: Claude failed (a reached cap too) or never gave a valid answer.
        """
        subject_name, topic_title = await asyncio.to_thread(
            _topic_names, vault, subject_slug, topic_slug
        )
        client = self.client_factory(LedgerBinding(vault, subject_slug, topic_slug, session_id))
        system = [self.prompt.content, render_topic(subject_name, topic_title, None)]
        context = await _context_of(self.sources_lookup, subject_slug, topic_slug)
        segment = _Final(MESSAGE_SEGMENT, text.strip(), 0, 0, self.clock.monotonic())
        rendered = (
            context.render() + "\n\nA typed message of the student, the single segment"
            f" `{MESSAGE_SEGMENT}`:\n{MESSAGE_SEGMENT} {segment.text}"
        )
        turn = {"role": "user", "content": [{"type": "text", "text": rendered}]}
        position = {MESSAGE_SEGMENT: 0}
        messages: list[dict[str, Any]] = [turn]
        accepted: list[ReportedRequest] = []
        checked = _Checked()
        for attempt in range(1 + client.structured_reasks):
            try:
                response = await client.create(
                    messages,
                    system=system,
                    tools=[self.tool],
                    tool_choice={"type": "auto"},
                    prompt_hash=self.prompt.hash,
                )
            except LLMError as error:
                raise ClassificationError(str(error)) from error
            await self._record(vault, subject_slug, topic_slug, messages[-1], response)
            checked = _check(response, position, set(), context, [segment], typed=True)
            accepted += [request for request, _span in checked.requests]
            if checked.called and not checked.errors:
                return accepted
            if attempt < client.structured_reasks:
                messages = [*messages, response.assistant_turn(), _reask_turn(response, checked)]
        if accepted:
            return accepted
        raise ClassificationError("; ".join(checked.errors or [f"no {TOOL_NAME} call"]))

    async def _record(
        self,
        vault: Any,
        subject_slug: str,
        topic_slug: str,
        message: dict[str, Any],
        response: LLMResponse,
    ) -> None:
        records = [
            ConversationRecord(time=self.clock.now(), kind="user", message=message),
            ConversationRecord(
                time=self.clock.now(),
                kind="assistant",
                message=response.assistant_turn(),
                model=response.model,
                prompt_hash=self.prompt.hash,
                usage=response.usage.model_dump(),
            ),
        ]
        for record in records:
            try:
                await asyncio.to_thread(
                    append_conversation_record,
                    vault,
                    subject_slug,
                    topic_slug,
                    MESSAGES_CONVERSATION,
                    record,
                )
            except SecretRefused:
                logger.warning(
                    "a typed message record of %s/%s looks like a secret", subject_slug, topic_slug
                )
            except (VaultError, OSError):
                logger.exception(
                    "the typed messages conversation of %s/%s cannot be written",
                    subject_slug,
                    topic_slug,
                )


def _topic_names(vault: Any, subject_slug: str, topic_slug: str) -> tuple[str, str]:
    return (
        get_subject(vault, subject_slug).subject.name,
        get_topic(vault, subject_slug, topic_slug).topic.title,
    )


def _ms(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int | float) and value >= 0 else max(0, default)


__all__ = [
    "DETECTOR",
    "MESSAGES_CONVERSATION",
    "MESSAGE_SEGMENT",
    "PROMPT_NAME",
    "TOOL_NAME",
    "ClassificationError",
    "Clock",
    "MessageClassifier",
    "ReportRequests",
    "ReportedRequest",
    "RequestDetector",
    "SystemClock",
    "requests_tool",
]
