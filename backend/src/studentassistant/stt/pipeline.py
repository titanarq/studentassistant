"""The transcript pipeline: bus `transcript.final` events -> the session's `transcript.jsonl`.

The WebSocket gateway publishes every final segment, from a client recognizer or a server-side
provider, as a `transcript.final` event on the session bus (already in session time, already
de-duplicated by `segment_id`). `TranscriptPipeline` subscribes to those and to `session.ended`,
runs one `TranscriptAssembler` per session (duplicates, covered spans, restarted-recognizer
overlaps and late finals) and appends each resulting segment with `Session.append_transcript`, the
vault write in a worker thread. Partials are never written.

It depends on the bus only through the `EventBus` protocol below and gets a session's vault handle
from an injected lookup callable, so `stt` never imports `studentassistant.server` (AGENTS.md's
dependency direction). Each appended segment logs its latency (logger
`studentassistant.stt.pipeline`): the session time of the append minus the segment's end, in ms.

Errors never reach the bus: a final of a session that has ended (`SessionEndedError`) or that has
no open handle is logged and dropped, a `SecretRefused` append is logged and skipped, and the
pipeline carries on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from studentassistant.stt.models import NormalisedSegment
from studentassistant.stt.transcript import TranscriptAssembler
from studentassistant.vault import SecretRefused, Session, SessionEndedError

logger = logging.getLogger(__name__)

TRANSCRIPT_FINAL = "transcript.final"
SESSION_ENDED = "session.ended"
PIPELINE_KINDS = frozenset({TRANSCRIPT_FINAL, SESSION_ENDED})

PIPELINE_QUEUE_SIZE = 1024
"""The pipeline's bus subscription bound (it only takes persisted events, which are never
dropped: the bound only decides when the bus logs that the pipeline lags)."""


class BusEventLike(Protocol):
    """What the pipeline reads of a bus event (`studentassistant.server.bus.BusEvent`)."""

    @property
    def session_id(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...


class SubscriptionLike(Protocol):
    """What the pipeline uses of a bus subscription."""

    def __aiter__(self) -> AsyncIterator[BusEventLike]: ...
    def __len__(self) -> int: ...
    def close(self) -> None: ...


class EventBus(Protocol):
    """The bus surface the pipeline uses (`studentassistant.server.bus.SessionBus` has it)."""

    def subscribe(
        self,
        *,
        name: str = ...,
        session_id: str | None = ...,
        kinds: Collection[str] | None = ...,
        maxsize: int | None = ...,
    ) -> SubscriptionLike: ...


SessionLookup = Callable[[str], Session | None]
"""The open vault handle of a session id, or None when the backend has none."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def segment_from_payload(payload: Mapping[str, Any]) -> NormalisedSegment:
    """The `NormalisedSegment` of a `transcript.final` payload (session milliseconds)."""
    return NormalisedSegment(
        start=payload["session_start_ms"] / 1000,
        end=payload["session_end_ms"] / 1000,
        text=payload["text"],
        provider=payload.get("provider", "unknown"),
        confidence=payload.get("confidence"),
        is_final=True,
    )


class _SessionTranscript:
    """One session's handle and assembler, seeded with what its transcript already holds."""

    def __init__(self, session: Session, assembler: TranscriptAssembler) -> None:
        self.session = session
        self.assembler = assembler


class TranscriptPipeline:
    """Consume the bus' `transcript.final` / `session.ended` events into `transcript.jsonl`.

    `start()` subscribes (events published from then on are seen) and runs the consumer task;
    `stop()` closes the subscription, processes what is still queued and ends the task. `drain()`
    waits until every event delivered so far has been processed. `clock` gives the current UTC
    time (for the latency log); tests replace it.
    """

    def __init__(
        self,
        bus: EventBus,
        lookup: SessionLookup,
        *,
        clock: Callable[[], datetime] = _utc_now,
        queue_size: int = PIPELINE_QUEUE_SIZE,
    ) -> None:
        self.bus = bus
        self.lookup = lookup
        self.clock = clock
        self.queue_size = queue_size
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._sessions: dict[str, _SessionTranscript] = {}
        self._ended: set[str] = set()
        self._busy = False
        self._settled = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Subscribe to the bus and start consuming; call it inside the running event loop."""
        if self._subscription is not None:
            return
        self._subscription = self.bus.subscribe(
            name="stt:transcript", kinds=PIPELINE_KINDS, maxsize=self.queue_size
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="stt:transcript")

    async def stop(self) -> None:
        """Stop receiving, write what was already delivered, then end the consumer task."""
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

    async def drain(self) -> None:
        """Return once every event delivered to the pipeline so far has been processed."""
        await asyncio.sleep(0)
        while (
            self.running
            and self._subscription is not None
            and (len(self._subscription) or self._busy)
        ):
            self._settled.clear()
            await self._settled.wait()

    # -- consumer ------------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            self._busy = True
            try:
                if event.kind == TRANSCRIPT_FINAL:
                    await self._on_final(event.session_id, event.payload)
                elif event.kind == SESSION_ENDED:
                    self._on_ended(event.session_id)
            except Exception:
                logger.exception(
                    "the transcript pipeline failed on a %s event of session %s",
                    event.kind,
                    event.session_id,
                )
            finally:
                self._busy = False
                if not len(subscription):
                    self._settled.set()

    def _on_ended(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._ended.add(session_id)

    async def _transcript_of(self, session_id: str) -> _SessionTranscript | None:
        current = self._sessions.get(session_id)
        if current is not None:
            return current
        session = self.lookup(session_id)
        if session is None:
            return None
        assembler = TranscriptAssembler()
        # A resumed session: what its transcript already holds is "written" for the rules.
        for written in await asyncio.to_thread(lambda: list(session.read_transcript())):
            assembler.seed(
                NormalisedSegment(
                    start=written.t_start / 1000,
                    end=written.t_end / 1000,
                    text=written.text,
                    provider="vault",
                )
            )
        current = self._sessions[session_id] = _SessionTranscript(session, assembler)
        return current

    async def _on_final(self, session_id: str, payload: Mapping[str, Any]) -> None:
        if session_id in self._ended:
            logger.warning(
                "a transcript.final of session %s arrived after its end; not written", session_id
            )
            return
        try:
            segment = segment_from_payload(payload)
        except (KeyError, TypeError, ValidationError):
            logger.warning("a transcript.final of session %s is malformed; skipped", session_id)
            return
        transcript = await self._transcript_of(session_id)
        if transcript is None:
            logger.error("no open vault session %s for a transcript.final; not written", session_id)
            return
        result = transcript.assembler.add(segment)
        if result is None:
            return
        t_start = round(result.start * 1000)
        t_end = max(t_start, round(result.end * 1000))
        session = transcript.session
        try:
            written = await asyncio.to_thread(
                session.append_transcript, t_start, t_end, result.text
            )
        except SessionEndedError:
            logger.error(
                "session %s ended before its final at %d ms was written; it is lost",
                session_id,
                t_start,
            )
            self._on_ended(session_id)
            return
        except SecretRefused:
            logger.warning(
                "a final of session %s at %d ms looks like a secret; not written",
                session_id,
                t_start,
            )
            return
        now_ms = int((self.clock() - session.meta.started_at) / timedelta(milliseconds=1))
        latency_ms = now_ms - t_end
        logger.info(
            "transcript segment %d of session %s (%s) appended, latency %d ms",
            written.seq,
            session_id,
            result.provider,
            latency_ms,
            extra={
                "session_id": session_id,
                "provider": result.provider,
                "latency_ms": latency_ms,
                "transcript_seq": written.seq,
            },
        )


__all__ = [
    "PIPELINE_KINDS",
    "EventBus",
    "SessionLookup",
    "TranscriptPipeline",
    "segment_from_payload",
]
