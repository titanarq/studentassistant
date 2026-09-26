"""Requests to the assistant, detected by Sonnet (role `observer`) in the raw transcript (#314).

`RequestDetector` subscribes to the session bus (`transcript.final` and the lifecycle events) and,
per active session, keeps the newest final segments and which of them it has examined. Once no new
final has arrived for `request_debounce_seconds`, or `request_max_wait_seconds` after the first
final it has not examined yet, it asks Claude whether the window of the newest
`request_window_segments` finals holds a request to the assistant. It runs apart from the batch
`ObserverLoop` (its own calls, its own trigger), so a request is never delayed by the loop's
batches. One call per session is in flight at a time; finals arriving meanwhile coalesce into the
next call.

Each call is self-contained: system = the `observer_requests` prompt + the topic block (subject,
title), the cached prefix together with the tool; one user turn = the window, each final marked
`new`, `seen` or with the request it already belongs to, so no segment is reported twice. The
answer must call the strict tool `report_requests` (`{requests: [{kind, summary, segment_ids}]}`).
Each request is checked (a Spanish `summary` of at most 140 characters, `segment_ids` consecutive
in the window and not already assigned); valid ones are published at once as persisted
`assistant.request` events (origin `observer`, payload `AssistantRequest`), the rest are re-asked
once and then dropped and logged.

Cost caps: the client is bound to the session's ledger. A reached cap pauses the detector: the
finals stay unexamined, one `observer.status` event (`status: paused`, `detector: "requests"`) is
published and the next final tries again; `status: running` follows a successful call. Any other
Claude failure does the same with `status: error` plus an `observer.call_failed` notice.

The conversation file is `conversations/observer-requests-<session-id>.jsonl` (a `context` record
when a session is first seen, then each `user` turn and `assistant` answer, and `status` changes).
`flush(session_id)` is the `add_before_ended` hook: it sends what is still unexamined at once and
waits for it, so a request spoken just before ending is still detected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

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
    ASSISTANT_REQUEST_KIND,
    SUMMARY_MAX_CHARS,
    AssistantRequest,
    RequestKind,
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
QUEUE_SIZE = 1024


class ReportedRequest(BaseModel):
    """One request of the `report_requests` tool input."""

    kind: RequestKind
    summary: str
    segment_ids: list[str]


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


class RequestDetector:
    """The app-wide request detector: `start()` subscribes, `stop()` ends it, `flush()` a session.

    Active only when `settings.request_detection == "observer"`; otherwise `start()` does nothing
    and no call is ever made. `lookup` gives an attached session's vault handle; `client_factory`
    builds a session's `observer` client from its ledger binding (tests pass
    `default_client_factory(transport=fake)`); `clock` gives the time (`SystemClock`).
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
    ) -> None:
        self.bus = bus
        self.lookup = lookup
        self.settings = settings or ObserverSettings()
        self.client_factory = client_factory or default_client_factory()
        self.clock: Clock = clock or SystemClock()
        self.queue_size = queue_size
        self.prompt = load_prompt(PROMPT_NAME)
        self.tool = requests_tool()
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._detected: dict[str, _Detected] = {}
        self._ignored: set[str] = set()
        self._busy = False
        self._settled = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.request_detection == "observer"

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
        self._subscription = self.bus.subscribe(
            name="observer-requests", kinds=REQUEST_KINDS_READ, maxsize=self.queue_size
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
            requests=len(earlier),
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
        earlier: list[AssistantRequest] = []
        for event in session.read_events():
            if event.kind != ASSISTANT_REQUEST_KIND:
                continue
            try:
                earlier.append(AssistantRequest.model_validate(event.payload))
            except ValidationError:
                logger.warning("session %s: an unreadable %s event", session.id, event.kind)
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
        turn = {
            "role": "user",
            "content": [{"type": "text", "text": self._render_window(detected, window, last)}],
        }
        response = await self._ask(detected, [turn])
        if response is None:
            detected.blocked = True
            return
        # Examined once answered, whatever the answer: a final is never examined twice as new.
        for final in examining:
            final.examined = True
        checked = self._check(detected, response, window)
        published = await self._publish(detected, checked)
        if checked.errors or not checked.called:
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
                        f"{published} requests were accepted. These were refused:\n"
                        + "\n".join(f"- {reason}" for reason in reasons)
                        + f"\nCall `{TOOL_NAME}` again with only the corrected requests (or an"
                        " empty list)."
                    ),
                }
            )
            retry_turn = {"role": "user", "content": content}
            retry = await self._ask(detected, [turn, response.assistant_turn(), retry_turn])
            if retry is None:
                return
            again = self._check(detected, retry, window)
            published += await self._publish(detected, again)
            if again.errors or not again.called:
                logger.warning(
                    "request detector of session %s: dropped after one re-ask: %s",
                    detected.id,
                    "; ".join(again.errors or [f"no {TOOL_NAME} call"]),
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

    @staticmethod
    def _check(detected: _Detected, response: LLMResponse, window: list[_Final]) -> _Checked:
        """The valid requests of `response`, in order; errors for the rest."""
        result = _Checked()
        if response.stop_reason == "refusal":
            result.called = True
            result.errors.append("the request was declined")
            return result
        position = {final.segment_id: index for index, final in enumerate(window)}
        taken = set(detected.assigned)
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
                problem = _span_problem(request, position, taken)
                if problem:
                    result.errors.append(f"request {index}: {problem}")
                    continue
                taken.update(request.segment_ids)
                span = [window[position[segment_id]] for segment_id in request.segment_ids]
                result.requests.append((request, span))
        return result

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
            )
            try:
                await self.bus.publish(
                    detected.id,
                    ASSISTANT_REQUEST_KIND,
                    OBSERVER_ORIGIN,
                    payload.model_dump(mode="json"),
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


def _span_problem(
    request: ReportedRequest, position: Mapping[str, int], taken: set[str]
) -> str | None:
    """Why a reported request's span is refused, or `None` when it is valid."""
    summary = request.summary.strip()
    if not summary:
        return "the summary is empty"
    if len(summary) > SUMMARY_MAX_CHARS:
        return f"the summary has {len(summary)} characters; at most {SUMMARY_MAX_CHARS}"
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


def _ms(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int | float) and value >= 0 else max(0, default)


__all__ = [
    "DETECTOR",
    "PROMPT_NAME",
    "TOOL_NAME",
    "Clock",
    "ReportRequests",
    "ReportedRequest",
    "RequestDetector",
    "SystemClock",
    "requests_tool",
]
