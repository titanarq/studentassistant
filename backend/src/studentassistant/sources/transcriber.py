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

Catch-up (#181, `catchup.py`): at every `session.started` / `session.resumed` the topic's log is
read for owed pages -- captures of any of its sessions, stored before the opening event, with no
recorded `page.transcribed`. One without `page-NNN.md` is queued at once (no window wait: its
transcript is stored; its hints come from its own session's `transcript.jsonl`); one whose
`page-NNN.md` is there has its events published from the Markdown (`recovered: true`).
`catch_up_vault(vault)` does the same once for every topic's unended and newest sessions (server
start: the server calls it when it first opens the vault); nothing is attached then, so those
events wait for the topic's next session.

Where the events go: to the page's own session while it is live, else to a live session of the same
topic, else nowhere yet -- the next start or resume of the topic records them (above). Hence a page
transcribed after its session ended has its events in a later session's `events.jsonl` of the same
topic, which both the observer's fold and the editor read, and `page.transcribed` /
`page.transcription_failed` carry `capture_session_id` (the capture's session). An `add_pending` id
already in the topic's log is never published again (the fold refuses a duplicate id).

The transcriber reaches the bus only through the protocols below (it never imports
`studentassistant.server`), Claude only through `studentassistant.llm` and the vault only through
`studentassistant.vault`.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
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
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    EventRef,
    op_payload,
)
from studentassistant.sources.catchup import (
    CAPTURE_SESSION_KEY,
    PAGE_TRANSCRIBED_KIND,
    PAGE_TRANSCRIPTION_FAILED_KIND,
    PageRef,
    read_owed,
)
from studentassistant.sources.transcription import (
    BOOK_KIND,
    PageInput,
    PageTranscription,
    find_uncertain,
    page_number,
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
    Vault,
    VaultError,
    append_conversation_record,
    list_sessions,
    list_subjects,
    list_topics,
    read_source,
)

logger = logging.getLogger(__name__)

SEGMENT_KIND = "transcript.final"
SESSION_STARTED = "session.started"
SESSION_RESUMED = "session.resumed"
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
    """One session's transcription jobs and the final segments seen on the bus.

    A session the bus does not know (server start: none is attached yet) gets a *detached* one,
    `ended` from the start: its jobs transcribe, and their events go to a live session of the
    topic or wait for the next one.
    """

    vault: Vault
    subject_slug: str
    topic_slug: str
    id: str
    client: LLMClient
    hurry: asyncio.Event = field(default_factory=asyncio.Event)
    segments: list[TranscriptSegment] = field(default_factory=list)
    jobs: dict[tuple[str, str], asyncio.Task[None]] = field(default_factory=dict)
    ended: bool = False

    @property
    def topic(self) -> tuple[str, str]:
        return self.subject_slug, self.topic_slug


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
        # (capture session, capture id) of every page a job is working on, whichever `_Pages`.
        self._inflight: set[tuple[str, str]] = set()
        # Per topic, every `add_pending` id known to be in its log (never published twice).
        self._pending_ids: dict[tuple[str, str], set[str]] = {}
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._startup: asyncio.Task[None] | None = None
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
            name="transcriber",
            kinds=(
                CAPTURE_EVENT_KIND,
                SEGMENT_KIND,
                SESSION_STARTED,
                SESSION_RESUMED,
                SESSION_ENDED,
            ),
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="transcriber")

    async def stop(self) -> None:
        """Stop listening and cancel every job still waiting or calling (shutdown)."""
        if self._subscription is not None:
            self._subscription.close()
        if self._task is not None:
            await self._task
            self._task = None
        startup, self._startup = self._startup, None
        if startup is not None:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        jobs = [job for pages in self._pages.values() for job in pages.jobs.values()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._pages.clear()
        self._inflight.clear()

    async def drain(self) -> None:
        """Wait until every event already delivered to the transcriber has been handled."""
        while self._subscription is not None and len(self._subscription) and self.running:
            await asyncio.sleep(0)

    def catch_up_vault(self, vault: Vault) -> None:
        """Server start: queue every topic's owed pages of its unended and newest sessions.

        Called once the vault is open (`SessionService.add_on_open`); runs in the background, once
        per `start()`, and only while the transcriber runs.
        """
        if not self.running or self._startup is not None:
            return
        self._startup = asyncio.create_task(self._catch_up_vault(vault), name="transcriber:startup")

    async def wait_startup(self) -> None:
        """Wait for the server-start catch-up to have queued its jobs (tests)."""
        if self._startup is not None:
            await asyncio.gather(self._startup, return_exceptions=True)

    async def wait_idle(self, session_id: str) -> None:
        """Wait (without hurrying) until every job of the session has finished."""
        await self.drain()
        pages = self._pages.get(session_id)
        if pages is not None:
            await _all_jobs(pages)

    async def flush(self, session_id: str) -> None:
        """End the session's waits and wait for its jobs (the `add_before_ended` hook)."""
        await self.drain()
        pages = self._pages.get(session_id)
        if pages is None:
            return
        pages.hurry.set()
        await asyncio.shield(_all_jobs(pages))

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
        if pages.ended:  # resumed while jobs of its earlier stretch (or startup's) were running
            pages.ended = False
            pages.hurry = asyncio.Event()
        payload = event.payload
        if event.kind in (SESSION_STARTED, SESSION_RESUMED):
            before = EventRef(session_id=pages.id, seq=event.seq)
            self._track(
                pages,
                ("", f"catch-up:{event.seq}"),
                self._catch_up(pages, before=before),
            )
            return
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
        if not isinstance(capture_id, str) or (pages.id, capture_id) in pages.jobs:
            return
        source_path = payload.get("source_path")
        if not isinstance(source_path, str):
            logger.warning("capture %s of session %s names no source_path", capture_id, pages.id)
            return
        page_path = payload.get("page_path")
        kind = payload.get("source_context")
        page = PageRef(
            session_id=pages.id,
            capture_id=capture_id,
            source_path=source_path,
            page_path=page_path if isinstance(page_path, str) else None,
            t=event.t,
            source_kind=kind if isinstance(kind, str) else None,
        )
        self._spawn(pages, page, wait=True)

    def _pages_of(self, session_id: str) -> _Pages | None:
        pages = self._pages.get(session_id)
        if pages is not None:
            return pages
        session = self.lookup(session_id)
        if session is None:
            return None
        return self._new_pages(session.vault, session.subject_slug, session.topic_slug, session.id)

    def _new_pages(
        self, vault: Vault, subject: str, topic: str, session_id: str, *, ended: bool = False
    ) -> _Pages:
        binding = LedgerBinding(vault, subject, topic, session_id)
        pages = _Pages(
            vault=vault,
            subject_slug=subject,
            topic_slug=topic,
            id=session_id,
            client=self.client_factory(binding),
            ended=ended,
        )
        self._pages[session_id] = pages
        return pages

    def _forget_if_done(self, pages: _Pages) -> None:
        done = pages.ended and all(job.done() for job in pages.jobs.values())
        if done and self._pages.get(pages.id) is pages:
            del self._pages[pages.id]

    def _track(self, pages: _Pages, key: tuple[str, str], work: Awaitable[None]) -> None:
        pages.jobs[key] = asyncio.create_task(
            self._guarded(pages, key, work), name=f"transcriber:{pages.id}:{key[1]}"
        )

    def _spawn(self, pages: _Pages, page: PageRef, *, wait: bool) -> bool:
        """Start a job for `page` unless one is working on it already."""
        if page.key in self._inflight:
            return False
        self._inflight.add(page.key)
        self._track(pages, page.key, self._transcribe(pages, page, wait=wait))
        return True

    async def _guarded(self, pages: _Pages, key: tuple[str, str], work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:
            logger.exception("transcriber job %s of session %s failed", key[1], pages.id)
        finally:
            if key[0]:
                self._inflight.discard(key)
            asyncio.get_running_loop().call_soon(self._forget_if_done, pages)

    # -- catch-up ------------------------------------------------------------------------------

    async def _catch_up(
        self,
        pages: _Pages,
        *,
        before: EventRef | None = None,
        sessions: Collection[str] | None = None,
    ) -> None:
        """Queue the topic's owed pages (`catchup.py`) and record the ones already transcribed."""
        try:
            owed = await asyncio.to_thread(
                read_owed,
                pages.vault,
                pages.subject_slug,
                pages.topic_slug,
                before=before,
                sessions=sessions,
            )
        except (VaultError, OSError):
            logger.exception("the transcriber cannot read topic %s/%s to catch up", *pages.topic)
            return
        self._pending_ids.setdefault(pages.topic, set()).update(owed.pending_ids)
        queued = sum(self._spawn(pages, page, wait=False) for page in owed.to_transcribe)
        recorded = 0
        for stored in owed.to_record:
            if stored.page.key in self._inflight:
                continue
            await self._deliver(pages, stored.page, stored.text, stored.path, recovered=True)
            recorded += 1
        if queued or recorded:
            logger.info(
                "transcriber of %s/%s: %d untranscribed pages queued, %d transcriptions recorded",
                *pages.topic,
                queued,
                recorded,
            )

    async def _catch_up_vault(self, vault: Vault) -> None:
        try:
            plan = await asyncio.to_thread(_startup_plan, vault)
        except (VaultError, OSError):
            logger.exception("the transcriber cannot read the vault to catch up at start")
            return
        for subject, topic, sessions in plan:
            newest = sessions[-1]
            pages = self._pages_of(newest) or self._new_pages(
                vault, subject, topic, newest, ended=True
            )
            self._track(
                pages, ("", "catch-up:startup"), self._catch_up(pages, sessions=set(sessions))
            )

    # -- one page ------------------------------------------------------------------------------

    async def _transcribe(self, pages: _Pages, ref: PageRef, *, wait: bool) -> None:
        if wait:
            window_end = await asyncio.to_thread(_window_end, pages.vault, ref.source_path)
            delay = max(0, (window_end if window_end is not None else ref.t) - ref.t) / 1000
            await self._pause(pages, delay + self.settings.transcription_grace_seconds)
        live_segments = ref.session_id == pages.id
        assert self._slots is not None
        attempts = 0
        async with self._slots:
            while True:
                attempts += 1
                try:
                    page = await asyncio.to_thread(
                        read_page_input,
                        pages.vault,
                        pages.subject_slug,
                        pages.topic_slug,
                        ref.session_id,
                        ref.source_path,
                        ref.page_path,
                        self.settings,
                        list(pages.segments) if live_segments else [],
                    )
                    result = await transcribe_page(pages.client, page, pages.vault, ref.source_path)
                    break
                except CostCapReachedError as error:
                    await self._failed(pages, ref, "cost_cap", str(error), attempts)
                    return
                except RefusalError as error:
                    await self._failed(pages, ref, "refused", str(error), attempts)
                    return
                except (LLMError, VaultError, OSError) as error:
                    if attempts >= self.settings.transcription_attempts:
                        await self._failed(pages, ref, "error", str(error), attempts)
                        return
                    logger.warning(
                        "transcription of capture %s (session %s), attempt %d failed: %s",
                        ref.capture_id,
                        ref.session_id,
                        attempts,
                        error,
                    )
                    retry = self.settings.transcription_retry_seconds * 2 ** (attempts - 1)
                    await self._pause(pages, retry)
        if self.on_write is not None:
            self.on_write()
        await self._record(pages, result, ref)
        input_ = result.page_input
        await self._deliver(
            pages,
            ref,
            result.text,
            result.path,
            source_kind=input_.source_kind,
            book_page=result.book_page.number if result.book_page is not None else None,
            extra={
                "original_sent": input_.original_image is not None,
                "hint_segments": len(input_.hints),
                "model": result.response.model,
                "prompt_hash": result.prompt_hash,
                "attempts": attempts,
                **(
                    {"book_page_from": result.book_page.number_from}
                    if result.book_page is not None
                    else {}
                ),
            },
        )

    async def _pause(self, pages: _Pages, seconds: float) -> None:
        if seconds <= 0 or pages.hurry.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pages.hurry.wait(), seconds)

    def _target(self, pages: _Pages) -> str | None:
        """The live session a page's events go to: its own, else one of the same topic."""
        candidates = [pages] + [
            other
            for other in self._pages.values()
            if other is not pages and other.topic == pages.topic and other.vault is pages.vault
        ]
        for candidate in candidates:
            if not candidate.ended and self.lookup(candidate.id) is not None:
                return candidate.id
        return None

    async def _deliver(
        self,
        pages: _Pages,
        ref: PageRef,
        text: str,
        path: str,
        *,
        source_kind: str | None = None,
        extra: Mapping[str, Any] | None = None,
        recovered: bool = False,
        book_page: int | None = None,
    ) -> None:
        """Publish a transcription's `add_pending` ops, then `page.transcribed`, to `_target`.

        With no live session of the topic the events wait: the next session start or resume of
        the topic finds the page owed and records it from its `page-NNN.md`.
        """
        kind = source_kind or ref.source_kind or PurePosixPath(ref.source_path).parent.name
        number = page_number(ref.source_path)
        book_meta: dict[str, Any] = {}
        if kind == BOOK_KIND:
            if book_page is None and recovered:
                book_meta = await asyncio.to_thread(_book_page_meta, pages.vault, ref.source_path)
                book_page = book_meta.get("book_page")
            book_meta["book_page"] = book_page
        uncertain = find_uncertain(text)
        ops = pending_ops(
            uncertain,
            session_id=ref.session_id,
            capture_id=ref.capture_id,
            source_kind=kind,
            page_number=number,
            book_page=book_page,
        )
        target = self._target(pages)
        if target is None:
            logger.info(
                "capture %s of session %s transcribed after its session ended; its events wait"
                " for the next session of %s/%s",
                ref.capture_id,
                ref.session_id,
                *pages.topic,
            )
            return
        known = self._pending_ids.setdefault(pages.topic, set())
        payload: dict[str, Any] = {
            "capture_id": ref.capture_id,
            CAPTURE_SESSION_KEY: ref.session_id,
            "text": text,
            "path": path,
            "source_path": ref.source_path,
            "page_path": ref.page_path,
            "source_context": kind,
            "page_number": number,
            "uncertain": len(uncertain),
            "pending_ids": [op.pending_id for op in ops],
            "recovered": recovered,
        }
        payload.update(book_meta)
        payload.update(extra or {})
        try:
            for op in ops:
                if op.pending_id in known:
                    continue
                await self.bus.publish(target, STATE_OP_EVENT_KIND, ORIGIN, op_payload(op))
                known.add(op.pending_id)
            await self.bus.publish(target, PAGE_TRANSCRIBED_KIND, ORIGIN, payload)
        except Exception:
            logger.exception(
                "the transcription of capture %s was written to %s but session %s took no event;"
                " the next session of the topic records it",
                ref.capture_id,
                path,
                target,
            )

    async def _failed(
        self, pages: _Pages, ref: PageRef, reason: str, message: str, attempts: int
    ) -> None:
        logger.warning(
            "capture %s of session %s not transcribed (%s): %s",
            ref.capture_id,
            ref.session_id,
            reason,
            message,
        )
        target = self._target(pages)
        if target is None:
            return
        try:
            await self.bus.publish(
                target,
                PAGE_TRANSCRIPTION_FAILED_KIND,
                ORIGIN,
                {
                    "capture_id": ref.capture_id,
                    CAPTURE_SESSION_KEY: ref.session_id,
                    "reason": reason,
                    "message": message,
                    "attempts": attempts,
                },
            )
        except Exception:
            logger.exception("session %s took no page.transcription_failed event", target)

    async def _record(self, pages: _Pages, result: PageTranscription, ref: PageRef) -> None:
        response = result.response
        records = [
            ConversationRecord(
                time=self.clock(),
                kind="user",
                message={
                    "role": "user",
                    "content": _recorded_content(result.page_input, ref.source_path, ref.page_path),
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
                    pages.vault,
                    pages.subject_slug,
                    pages.topic_slug,
                    CONVERSATION_PREFIX + ref.session_id,
                    record,
                )
        except SecretRefused:
            logger.warning("a transcriber record of session %s looks like a secret", ref.session_id)
        except (VaultError, OSError):
            logger.exception(
                "the transcriber conversation of session %s cannot be written", ref.session_id
            )


async def _all_jobs(pages: _Pages) -> None:
    """Wait until every job of `pages` is done, including those started meanwhile (catch-up)."""
    while pending := [job for job in pages.jobs.values() if not job.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def _startup_plan(vault: Vault) -> list[tuple[str, str, list[str]]]:
    """Per topic with study sessions: its unended ones and its newest one, in id order.

    Review sessions (a doubt's resolution, #191) hold no captures, so they are left out.
    """
    plan: list[tuple[str, str, list[str]]] = []
    for subject in list_subjects(vault):
        for topic in list_topics(vault, subject.slug):
            metas = [
                meta for meta in list_sessions(vault, subject.slug, topic.slug) if meta.is_study
            ]
            if not metas:
                continue
            chosen = {meta.id for meta in metas if meta.ended_at is None} | {metas[-1].id}
            plan.append((subject.slug, topic.slug, sorted(chosen)))
    return plan


def _window_end(vault: Vault, source_path: str) -> int | None:
    """The end (session ms) of the capture's transcript window, from its sidecar."""
    try:
        meta = read_source(vault, source_path).meta or {}
    except (VaultError, OSError):
        return None
    window = meta.get("transcript_window")
    if isinstance(window, Mapping) and isinstance(window.get("t_end"), int):
        return int(window["t_end"])
    return None


def _book_page_meta(vault: Vault, source_path: str) -> dict[str, Any]:
    """`book_page` and `book_page_from` of a textbook page's sidecar (a page recorded again)."""
    try:
        meta = read_source(vault, source_path).meta or {}
    except (VaultError, OSError):
        return {}
    number = meta.get("book_page")
    found: dict[str, Any] = {"book_page": number if isinstance(number, int) else None}
    if isinstance(meta.get("book_page_from"), str):
        found["book_page_from"] = meta["book_page_from"]
    return found


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
    "CAPTURE_SESSION_KEY",
    "CONVERSATION_PREFIX",
    "PAGE_TRANSCRIBED_KIND",
    "PAGE_TRANSCRIPTION_FAILED_KIND",
    "PageTranscriber",
    "default_client_factory",
]
