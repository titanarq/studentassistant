"""The web searcher: runs "busca esto en Internet" requests in the background and keeps pages.

`WebSearcher` subscribes to the session bus for `voice.command` events whose `command` is
`web_search` (the stt grammar's command, #47: payload `query`, `segment_id`, `text`) and queues a
search for the session's topic; the web review UI queues one through `submit` (the server's
`POST .../web-searches`). A search never blocks the session: `submit` only records it as
`search.queued` in the topic's `conversations/web-search.jsonl` and starts a job, which waits for
one of `[sources] web_search_concurrency` slots and runs `web.search_web` with a client of role
`[sources] web_search_role` bound to the topic's ledger (the session's, when one asked), so it is
capped and recorded like every other call.

When a job ends it records `search.results` (or `search.failed` with `reason` `cost_cap` |
`refused` | `error`) and, when the session that asked is still live, publishes
`web.search_results` / `web.search_failed` on it (origin `observer`). With `[sources]
web_auto_keep`, the results Claude marked `relevant` are kept at once (`kept_by: assistant`).

`keep(...)` keeps one offered result: it fetches the page (`web.snapshot_page`), stores it as
`sources/web/NNN-<slug>.md` (`web.keep_snapshot`), records `search.kept`, tells the vault sync a
file was written (`on_write`) and, when the topic has a live session, publishes
`web.snapshot_stored` on it (`search_id`, `index`, `url`, `source_id`, `title`, `kept_by`), so the
observer and the editor know a new external source is there. Keeping the same result twice
returns the first snapshot.

The searcher reaches the bus only through the protocols of `sources.transcriber` (it never
imports `studentassistant.server`), Claude only through `studentassistant.llm` and the vault only
through `studentassistant.vault`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from studentassistant.config import Settings, SourcesSettings
from studentassistant.llm import (
    CostCapError,
    LedgerBinding,
    LLMClient,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.sources.transcriber import BusEventLike, EventBus, SubscriptionLike
from studentassistant.sources.web import (
    WEB_SEARCH_FAILED_KIND,
    WEB_SEARCH_RESULTS_KIND,
    WEB_SNAPSHOT_STORED_KIND,
    FailureReason,
    KeptBy,
    KeptWebSource,
    RequestedBy,
    WebFetchError,
    WebSearchRecord,
    find_web_search,
    keep_snapshot,
    list_web_searches,
    new_search_id,
    record_failed,
    record_kept,
    record_queued,
    record_results,
    search_web,
    snapshot_page,
)
from studentassistant.vault import (
    SecretRefused,
    Session,
    Vault,
    VaultError,
    get_subject,
    get_topic,
    topic_directory,
)

logger = logging.getLogger(__name__)

VOICE_COMMAND_KIND = "voice.command"
WEB_SEARCH_COMMAND = "web_search"
ORIGIN = "observer"

ClientFactory = Callable[[LedgerBinding], LLMClient]


def default_client_factory(
    settings: Settings | None = None, transport: Any | None = None
) -> ClientFactory:
    """Clients of role `[sources] web_search_role`, capped and recorded on the ledger."""
    resolved = settings or Settings()

    def build(binding: LedgerBinding) -> LLMClient:
        return get_client(
            resolved.sources.web_search_role,
            settings=resolved,
            transport=transport,
            ledger=binding,
        )

    return build


class KeepError(Exception):
    """A result that cannot be kept; `status` is the HTTP status, the message Spanish."""

    status = 422


class UnknownSearchError(KeepError):
    status = 404


class SearchNotDoneError(KeepError):
    status = 409


@dataclass(frozen=True)
class _Topic:
    vault: Vault
    subject: str
    topic: str

    @property
    def key(self) -> tuple[str, str, str]:
        return str(self.vault.path), self.subject, self.topic


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WebSearcher:
    """Runs web searches in the background and keeps their pages (see the module docstring)."""

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
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._keep_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._slots: asyncio.Semaphore | None = None
        self._handling = 0

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._slots = asyncio.Semaphore(self.settings.web_search_concurrency)
        self._subscription = self.bus.subscribe(name="web-searcher", kinds=(VOICE_COMMAND_KIND,))
        self._task = asyncio.create_task(self._run(self._subscription), name="web-searcher")

    async def stop(self) -> None:
        """Stop listening and cancel every search still waiting or running (shutdown)."""
        if self._subscription is not None:
            self._subscription.close()
        if self._task is not None:
            await self._task
            self._task = None
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()

    async def drain(self) -> None:
        """Wait until every event already delivered to the searcher has been handled."""
        while self.running and self._subscription is not None:
            if not len(self._subscription) and not self._handling:
                return
            await asyncio.sleep(0.01)

    async def wait_idle(self) -> None:
        """Wait until every search queued so far has ended (tests)."""
        await self.drain()
        while pending := [job for job in self._jobs.values() if not job.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def in_flight(self, search_id: str) -> bool:
        job = self._jobs.get(search_id)
        return job is not None and not job.done()

    # -- voice commands ------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            self._handling += 1
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("the web searcher failed on an event of %s", event.session_id)
            finally:
                self._handling -= 1

    async def _on_event(self, event: BusEventLike) -> None:
        payload = event.payload
        if event.seq is None or payload.get("command") != WEB_SEARCH_COMMAND:
            return
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return
        session = self.lookup(event.session_id)
        if session is None:
            return
        await self.submit(
            session.vault,
            session.subject_slug,
            session.topic_slug,
            query,
            requested_by="voice",
            session_id=session.id,
        )

    # -- searching -----------------------------------------------------------------------------

    async def submit(
        self,
        vault: Vault,
        subject_slug: str,
        topic_slug: str,
        query: str,
        *,
        requested_by: RequestedBy = "web",
        session_id: str | None = None,
    ) -> str:
        """Record a search as queued, start it in the background and return its id.

        Raises `ValueError` for an empty query and the vault's topic errors for an unknown topic.
        """
        query = query.strip()
        if not query:
            raise ValueError("empty web search query")
        if not self.running:
            self.start()
        now = self.clock()
        search_id = new_search_id(now)
        await asyncio.to_thread(
            record_queued,
            vault,
            subject_slug,
            topic_slug,
            search_id=search_id,
            query=query,
            requested_by=requested_by,
            session_id=session_id,
            now=now,
        )
        self._wrote()
        where = _Topic(vault, subject_slug, topic_slug)
        self._jobs[search_id] = asyncio.create_task(
            self._search(where, search_id, query, requested_by, session_id),
            name=f"web-search:{search_id}",
        )
        return search_id

    async def _search(
        self,
        where: _Topic,
        search_id: str,
        query: str,
        requested_by: str,
        session_id: str | None,
    ) -> None:
        assert self._slots is not None
        try:
            async with self._slots:
                subject_name, topic_title = await asyncio.to_thread(_names, where)
                client = self.client_factory(
                    LedgerBinding(where.vault, where.subject, where.topic, session_id)
                )
                search = await search_web(
                    client, query, settings=self.settings, subject=subject_name, topic=topic_title
                )
        except asyncio.CancelledError:
            raise
        except CostCapError as error:
            await self._failed(where, search_id, query, session_id, "cost_cap", str(error))
            return
        except RefusalError as error:
            await self._failed(where, search_id, query, session_id, "refused", str(error))
            return
        except (LLMError, VaultError, OSError) as error:
            await self._failed(where, search_id, query, session_id, "error", str(error))
            return
        except Exception as error:  # never leave a search queued forever
            logger.exception("web search %s failed", search_id)
            await self._failed(where, search_id, query, session_id, "error", str(error))
            return
        try:
            await asyncio.to_thread(
                record_results,
                where.vault,
                where.subject,
                where.topic,
                search_id=search_id,
                search=search,
                now=self.clock(),
            )
        except (VaultError, OSError):
            logger.exception("web search %s: the results cannot be recorded", search_id)
            return
        self._wrote()
        await self._publish(
            session_id,
            WEB_SEARCH_RESULTS_KIND,
            {
                "search_id": search_id,
                "query": query,
                "requested_by": requested_by,
                "results": [result.model_dump() for result in search.results],
                "model": search.model,
            },
        )
        if self.settings.web_auto_keep:
            for index, result in enumerate(search.results):
                if not result.relevant:
                    continue
                try:
                    await self.keep(
                        where.vault,
                        where.subject,
                        where.topic,
                        search_id,
                        index,
                        kept_by="assistant",
                        session_id=session_id,
                    )
                except (KeepError, LLMError, VaultError, OSError) as error:
                    logger.warning("web search %s: result %d not kept: %s", search_id, index, error)

    async def _failed(
        self,
        where: _Topic,
        search_id: str,
        query: str,
        session_id: str | None,
        reason: FailureReason,
        message: str,
    ) -> None:
        logger.warning("web search %s failed (%s): %s", search_id, reason, message)
        try:
            await asyncio.to_thread(
                record_failed,
                where.vault,
                where.subject,
                where.topic,
                search_id=search_id,
                reason=reason,
                message=message,
                now=self.clock(),
            )
            self._wrote()
        except (VaultError, OSError):
            logger.exception("web search %s: the failure cannot be recorded", search_id)
        await self._publish(
            session_id,
            WEB_SEARCH_FAILED_KIND,
            {"search_id": search_id, "query": query, "reason": reason, "message": message},
        )

    # -- listing and keeping -------------------------------------------------------------------

    async def list(self, vault: Vault, subject_slug: str, topic_slug: str) -> list[WebSearchRecord]:
        """The topic's searches, newest first; a queued one no job runs is `failed/interrupted`
        (the server stopped while it ran)."""
        searches = await asyncio.to_thread(list_web_searches, vault, subject_slug, topic_slug)
        for search in searches:
            if search.status == "queued" and not self.in_flight(search.search_id):
                search.status = "failed"
                search.reason = "interrupted"
                search.message = "La búsqueda se interrumpió; vuelve a pedirla."
        return searches

    async def keep(
        self,
        vault: Vault,
        subject_slug: str,
        topic_slug: str,
        search_id: str,
        index: int,
        *,
        kept_by: KeptBy = "student",
        session_id: str | None = None,
    ) -> KeptWebSource:
        """Fetch and store result `index` of search `search_id` as a web source of the topic.

        Raises `UnknownSearchError` (no such search or result), `SearchNotDoneError` (it has no
        results yet), `KeepError` (the page cannot be fetched as text, or it looks like it
        carries a key) and the client's `LLMError`s (a reached cost cap included).
        """
        where = _Topic(vault, subject_slug, topic_slug)
        lock = self._keep_locks.setdefault(where.key, asyncio.Lock())
        async with lock:
            search = await asyncio.to_thread(
                find_web_search, vault, subject_slug, topic_slug, search_id
            )
            if search is None:
                raise UnknownSearchError("No existe esa búsqueda en este tema.")
            if search.status != "done":
                raise SearchNotDoneError("La búsqueda todavía no tiene resultados.")
            if not 0 <= index < len(search.results):
                raise UnknownSearchError("Esa búsqueda no tiene ese resultado.")
            for kept in search.kept:
                if kept.index == index:
                    return KeptWebSource(
                        path=topic_directory(vault, subject_slug, topic_slug) / kept.source_id,
                        source_id=kept.source_id,
                        title=search.results[index].title,
                        url=kept.url,
                    )
            result = search.results[index]
            session_id = session_id or search.session_id
            client = self.client_factory(LedgerBinding(vault, subject_slug, topic_slug, session_id))
            try:
                snapshot = await snapshot_page(
                    client, result.url, settings=self.settings, now=self.clock()
                )
            except WebFetchError as error:
                raise KeepError(str(error)) from error
            live_session = session_id if session_id and self.lookup(session_id) else None
            try:
                stored = await asyncio.to_thread(
                    keep_snapshot,
                    vault,
                    subject_slug,
                    topic_slug,
                    snapshot,
                    result=result,
                    search_id=search_id,
                    query=search.query,
                    kept_by=kept_by,
                    session_id=live_session,
                )
            except SecretRefused as error:
                raise KeepError(
                    "La página parece contener una clave o un token y no se ha guardado."
                ) from error
            await asyncio.to_thread(
                record_kept,
                vault,
                subject_slug,
                topic_slug,
                search_id=search_id,
                index=index,
                kept=stored,
                kept_by=kept_by,
                now=self.clock(),
            )
        self._wrote()
        await self._publish(
            session_id,
            WEB_SNAPSHOT_STORED_KIND,
            {
                "search_id": search_id,
                "index": index,
                "url": stored.url,
                "source_id": stored.source_id,
                "title": stored.title,
                "kept_by": kept_by,
            },
        )
        return stored

    # -- helpers -------------------------------------------------------------------------------

    def _wrote(self) -> None:
        if self.on_write is not None:
            self.on_write()

    async def _publish(self, session_id: str | None, kind: str, payload: Mapping[str, Any]) -> None:
        """Publish on the session that asked while it is live; otherwise the record is enough."""
        if session_id is None or self.lookup(session_id) is None:
            return
        try:
            await self.bus.publish(session_id, kind, ORIGIN, payload)
        except Exception:
            logger.exception("session %s took no %s event", session_id, kind)


def _names(where: _Topic) -> tuple[str | None, str | None]:
    try:
        subject = get_subject(where.vault, where.subject).subject.name
    except (VaultError, OSError):
        subject = None
    try:
        topic = get_topic(where.vault, where.subject, where.topic).topic.title
    except (VaultError, OSError):
        topic = None
    return subject, topic


__all__ = [
    "VOICE_COMMAND_KIND",
    "WEB_SEARCH_COMMAND",
    "KeepError",
    "SearchNotDoneError",
    "UnknownSearchError",
    "WebSearcher",
    "default_client_factory",
]
