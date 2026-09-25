"""The page transcriber: every stored capture of a live session is transcribed in the background.

`PageTranscriber` subscribes to the session bus. For each persisted `capture.stored` event (whose
payload names the stored still, `source_path`, and its page image, `page_path`) it starts one job:

1. wait until the capture's transcript window is over (its sidecar's `transcript_window.t_end`,
   plus `transcription_grace_seconds` for the last final segment to arrive), so the spoken hints
   after the photo are there too; the end of the session cuts the wait short;
2. take one of `transcription_concurrency` slots and call `transcription.transcribe_page` with the
   session's `transcriber` client (bound to the session's cost ledger, so every call is capped and
   recorded); the hints are the transcript segments of `transcript.jsonl` plus the
   `transcript.final` events seen on the bus that are not written yet;
3. on success, write `page-NNN.md`, record the exchange in
   `conversations/transcriber-<session>.jsonl` (the images as vault paths, never their bytes),
   publish one `observer.state_op` `add_pending` (category `illegible`, the capture as its ref)
   per `[[?...]]` mark, then `page.transcribed` (`capture_id`, `text`, `path`, ...), which the
   observer reads. Both have origin `observer`, ADR-0003's origin for what understands the
   session.

A failed attempt (any `LLMError` but a reached cost cap or a refusal, or a vault error) is retried
up to `transcription_attempts` times, `transcription_retry_seconds` apart (doubling each time).
When every attempt failed, or at once for a cost cap or a refusal, `page.transcription_failed`
(`capture_id`, `reason`: `cost_cap` | `refused` | `error`, `message`, `attempts`) is published and
the job ends. Nothing here ever blocks the bus or the capture: the consumer only schedules jobs.

Ending: `flush(session_id)` is an `add_before_ended` hook (registered before the observer's, so the
observer sees the transcriptions): it ends the waits and waits for the session's jobs, which are
shielded, so the end hook's timeout never cancels a call half-way.

The transcriber reaches the bus only through the protocols below (it never imports
`studentassistant.server`), Claude only through `studentassistant.llm` and the vault only through
`studentassistant.vault`.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from studentassistant.config import Settings, SourcesSettings
from studentassistant.llm import (
    CostCapReachedError,
    LedgerBinding,
    LLMClient,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.observer import CAPTURE_EVENT_KIND, STATE_OP_EVENT_KIND, op_payload
from studentassistant.sources.transcription import (
    PageInput,
    PageTranscription,
    pending_ops,
    read_page_input,
    render_request_text,
    transcribe_page,
)
from studentassistant.vault import (
    ConversationRecord,
    SecretRefused,
    Session,
    TranscriptSegment,
    VaultError,
    append_conversation_record,
    read_source,
)

logger = logging.getLogger(__name__)

PAGE_TRANSCRIBED_KIND = "page.transcribed"
PAGE_TRANSCRIPTION_FAILED_KIND = "page.transcription_failed"
SEGMENT_KIND = "transcript.final"
SESSION_ENDED = "session.ended"
ORIGIN = "observer"
CONVERSATION_PREFIX = "transcriber-"

ClientFactory = Callable[[LedgerBinding], LLMClient]


class BusEventLike(Protocol):
    @property
    def session_id(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def t(self) -> int: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...
    @property
    def seq(self) -> int | None: ...


class SubscriptionLike(Protocol):
    def __aiter__(self) -> AsyncIterator[BusEventLike]: ...
    def __len__(self) -> int: ...
    def close(self) -> None: ...


class EventBus(Protocol):
    """What the transcriber needs of `server.bus.SessionBus`."""

    def subscribe(
        self,
        *,
        name: str = "",
        session_id: str | None = None,
        kinds: Collection[str] | None = None,
        maxsize: int | None = None,
    ) -> SubscriptionLike: ...

    def publish(
        self,
        session_id: str,
        kind: str,
        origin: Any,
        payload: Mapping[str, Any] | None = None,
        *,
        persist: bool = True,
        t: int | None = None,
    ) -> Any: ...


def default_client_factory(
    settings: Settings | None = None, transport: Any | None = None
) -> ClientFactory:
    """Transcriber clients from `[llm.roles.transcriber]`, capped and recorded on the ledger."""

    def build(binding: LedgerBinding) -> LLMClient:
        return get_client("transcriber", settings=settings, transport=transport, ledger=binding)

    return build


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Pages:
    """One session's transcription jobs and the final segments seen on the bus."""

    session: Session
    client: LLMClient
    hurry: asyncio.Event = field(default_factory=asyncio.Event)
    segments: list[TranscriptSegment] = field(default_factory=list)
    jobs: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    ended: bool = False

    @property
    def id(self) -> str:
        return self.session.id


class PageTranscriber:
    """Transcribes every page stored in a live session (see the module docstring)."""

    def __init__(
        self,
        bus: EventBus,
        lookup: Callable[[str], Session | None],
        *,
        settings: SourcesSettings | None = None,
        client_factory: ClientFactory | None = None,
        on_write: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.bus = bus
        self.lookup = lookup
        self.settings = settings or SourcesSettings()
        self.client_factory = client_factory or default_client_factory()
        self.on_write = on_write
        self.clock = clock
        self._pages: dict[str, _Pages] = {}
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._slots: asyncio.Semaphore | None = None

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._slots = asyncio.Semaphore(self.settings.transcription_concurrency)
        self._subscription = self.bus.subscribe(
            name="transcriber", kinds=(CAPTURE_EVENT_KIND, SEGMENT_KIND, SESSION_ENDED)
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="transcriber")

    async def stop(self) -> None:
        """Stop listening and cancel every job still waiting or calling (shutdown)."""
        if self._subscription is not None:
            self._subscription.close()
        if self._task is not None:
            await self._task
            self._task = None
        jobs = [job for pages in self._pages.values() for job in pages.jobs.values()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._pages.clear()

    async def drain(self) -> None:
        """Wait until every event already delivered to the transcriber has been handled."""
        while self._subscription is not None and len(self._subscription) and self.running:
            await asyncio.sleep(0)

    async def wait_idle(self, session_id: str) -> None:
        """Wait (without hurrying) until every job of the session has finished."""
        await self.drain()
        pages = self._pages.get(session_id)
        if pages is not None:
            await asyncio.gather(*pages.jobs.values(), return_exceptions=True)

    async def flush(self, session_id: str) -> None:
        """End the session's waits and wait for its jobs (the `add_before_ended` hook)."""
        await self.drain()
        pages = self._pages.get(session_id)
        if pages is None:
            return
        pages.hurry.set()
        jobs = list(pages.jobs.values())
        if jobs:
            await asyncio.shield(asyncio.gather(*jobs, return_exceptions=True))

    # -- events --------------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            try:
                self._on_event(event)
            except Exception:
                logger.exception(
                    "the transcriber failed on a %s event of session %s",
                    event.kind,
                    event.session_id,
                )

    def _on_event(self, event: BusEventLike) -> None:
        if event.kind == SESSION_ENDED:
            pages = self._pages.get(event.session_id)
            if pages is not None:
                pages.ended = True
                pages.hurry.set()
                self._forget_if_done(pages)
            return
        if event.seq is None:
            return
        pages = self._pages_of(event.session_id)
        if pages is None:
            return
        if pages.ended:  # resumed while jobs of its earlier stretch were still running
            pages.ended = False
            pages.hurry = asyncio.Event()
        payload = event.payload
        if event.kind == SEGMENT_KIND:
            try:
                pages.segments.append(
                    TranscriptSegment(
                        seq=event.seq,
                        t_start=int(payload["session_start_ms"]),
                        t_end=int(payload["session_end_ms"]),
                        text=str(payload["text"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("a malformed transcript.final of session %s", pages.id)
            return
        capture_id = payload.get("capture_id")
        if not isinstance(capture_id, str) or capture_id in pages.jobs:
            return
        pages.jobs[capture_id] = asyncio.create_task(
            self._job(pages, capture_id, event), name=f"transcriber:{pages.id}:{capture_id}"
        )

    def _pages_of(self, session_id: str) -> _Pages | None:
        pages = self._pages.get(session_id)
        if pages is not None:
            return pages
        session = self.lookup(session_id)
        if session is None:
            return None
        binding = LedgerBinding(session.vault, session.subject_slug, session.topic_slug, session.id)
        pages = _Pages(session=session, client=self.client_factory(binding))
        self._pages[session_id] = pages
        return pages

    def _forget_if_done(self, pages: _Pages) -> None:
        done = pages.ended and all(job.done() for job in pages.jobs.values())
        if done and self._pages.get(pages.id) is pages:
            del self._pages[pages.id]

    # -- one page ------------------------------------------------------------------------------

    async def _job(self, pages: _Pages, capture_id: str, event: BusEventLike) -> None:
        try:
            await self._transcribe(pages, capture_id, event)
        except Exception:
            logger.exception(
                "the transcription of capture %s of session %s failed", capture_id, pages.id
            )
        finally:
            asyncio.get_running_loop().call_soon(self._forget_if_done, pages)

    async def _transcribe(self, pages: _Pages, capture_id: str, event: BusEventLike) -> None:
        source_path = event.payload.get("source_path")
        page_path = event.payload.get("page_path")
        if not isinstance(source_path, str):
            logger.warning("capture %s of session %s names no source_path", capture_id, pages.id)
            return
        if not isinstance(page_path, str):
            page_path = None
        session = pages.session
        window_end = await asyncio.to_thread(_window_end, session, source_path)
        delay = max(0, (window_end if window_end is not None else event.t) - event.t) / 1000
        await self._pause(pages, delay + self.settings.transcription_grace_seconds)
        assert self._slots is not None
        attempts = 0
        async with self._slots:
            while True:
                attempts += 1
                try:
                    page = await asyncio.to_thread(
                        read_page_input,
                        session.vault,
                        session.subject_slug,
                        session.topic_slug,
                        session.id,
                        source_path,
                        page_path,
                        self.settings,
                        list(pages.segments),
                    )
                    result = await transcribe_page(pages.client, page, session.vault, source_path)
                    break
                except CostCapReachedError as error:
                    await self._failed(pages, capture_id, "cost_cap", str(error), attempts)
                    return
                except RefusalError as error:
                    await self._failed(pages, capture_id, "refused", str(error), attempts)
                    return
                except (LLMError, VaultError, OSError) as error:
                    if attempts >= self.settings.transcription_attempts:
                        await self._failed(pages, capture_id, "error", str(error), attempts)
                        return
                    logger.warning(
                        "transcription of capture %s (session %s), attempt %d failed: %s",
                        capture_id,
                        pages.id,
                        attempts,
                        error,
                    )
                    retry = self.settings.transcription_retry_seconds * 2 ** (attempts - 1)
                    await self._pause(pages, retry)
        if self.on_write is not None:
            self.on_write()
        await self._record(pages, result, source_path, page_path)
        await self._publish_result(pages, capture_id, result, source_path, page_path, attempts)

    async def _pause(self, pages: _Pages, seconds: float) -> None:
        if seconds <= 0 or pages.hurry.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pages.hurry.wait(), seconds)

    async def _publish_result(
        self,
        pages: _Pages,
        capture_id: str,
        result: PageTranscription,
        source_path: str,
        page_path: str | None,
        attempts: int,
    ) -> None:
        page = result.page_input
        ops = pending_ops(
            result.uncertain,
            session_id=pages.id,
            capture_id=capture_id,
            source_kind=page.source_kind,
            page_number=page.page_number,
        )
        try:
            for op in ops:
                await self.bus.publish(pages.id, STATE_OP_EVENT_KIND, ORIGIN, op_payload(op))
            await self.bus.publish(
                pages.id,
                PAGE_TRANSCRIBED_KIND,
                ORIGIN,
                {
                    "capture_id": capture_id,
                    "text": result.text,
                    "path": result.path,
                    "source_path": source_path,
                    "page_path": page_path,
                    "source_context": page.source_kind,
                    "page_number": page.page_number,
                    "original_sent": page.original_image is not None,
                    "hint_segments": len(page.hints),
                    "uncertain": len(result.uncertain),
                    "pending_ids": [op.pending_id for op in ops],
                    "model": result.response.model,
                    "prompt_hash": result.prompt_hash,
                    "attempts": attempts,
                },
            )
        except Exception:
            logger.exception(
                "the transcription of capture %s was written to %s but session %s took no event",
                capture_id,
                result.path,
                pages.id,
            )

    async def _failed(
        self, pages: _Pages, capture_id: str, reason: str, message: str, attempts: int
    ) -> None:
        logger.warning(
            "capture %s of session %s not transcribed (%s): %s",
            capture_id,
            pages.id,
            reason,
            message,
        )
        try:
            await self.bus.publish(
                pages.id,
                PAGE_TRANSCRIPTION_FAILED_KIND,
                ORIGIN,
                {
                    "capture_id": capture_id,
                    "reason": reason,
                    "message": message,
                    "attempts": attempts,
                },
            )
        except Exception:
            logger.exception("session %s took no page.transcription_failed event", pages.id)

    async def _record(
        self,
        pages: _Pages,
        result: PageTranscription,
        source_path: str,
        page_path: str | None,
    ) -> None:
        session = pages.session
        response = result.response
        records = [
            ConversationRecord(
                time=self.clock(),
                kind="user",
                message={
                    "role": "user",
                    "content": _recorded_content(result.page_input, source_path, page_path),
                },
            ),
            ConversationRecord(
                time=self.clock(),
                kind="assistant",
                message=copy.deepcopy(response.assistant_turn()),
                model=response.model,
                prompt_hash=result.prompt_hash,
                usage=response.usage.model_dump(),
            ),
        ]
        try:
            for record in records:
                await asyncio.to_thread(
                    append_conversation_record,
                    session.vault,
                    session.subject_slug,
                    session.topic_slug,
                    CONVERSATION_PREFIX + session.id,
                    record,
                )
        except SecretRefused:
            logger.warning("a transcriber record of session %s looks like a secret", pages.id)
        except (VaultError, OSError):
            logger.exception(
                "the transcriber conversation of session %s cannot be written", pages.id
            )


def _window_end(session: Session, source_path: str) -> int | None:
    """The end (session ms) of the capture's transcript window, from its sidecar."""
    try:
        meta = read_source(session.vault, source_path).meta or {}
    except (VaultError, OSError):
        return None
    window = meta.get("transcript_window")
    if isinstance(window, Mapping) and isinstance(window.get("t_end"), int):
        return int(window["t_end"])
    return None


def _recorded_content(
    page: PageInput, source_path: str, page_path: str | None
) -> list[dict[str, Any]]:
    """The request's blocks as the conversation file keeps them: images by vault path."""
    blocks: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "vault", "path": page_path or source_path}}
    ]
    if page.original_image is not None:
        blocks.append({"type": "image", "source": {"type": "vault", "path": source_path}})
    blocks.append({"type": "text", "text": render_request_text(page)})
    return blocks


__all__ = [
    "CONVERSATION_PREFIX",
    "PAGE_TRANSCRIBED_KIND",
    "PAGE_TRANSCRIPTION_FAILED_KIND",
    "PageTranscriber",
    "default_client_factory",
]
