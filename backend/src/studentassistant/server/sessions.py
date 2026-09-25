"""The session lifecycle service: subjects, topics and the `active` -> `ended` session machine.

A session belongs to exactly one topic of one subject, fixed when it starts (ADR-0003); switching
topic means ending the session and starting another. The backend has at most one active session
at a time: starting one while any session is still unended is refused, and so is resuming one
while another is active. An unended session found in the vault (the backend stopped without
ending it) blocks a new start until it is resumed or ended. Resuming continues its logs' `seq`
where they stopped.

Every lifecycle change is itself published on the `SessionBus` as a persisted event
(`session.started`, `session.resumed`, `session.ended`, origin `user`); ending a session then
checkpoints the vault and pushes it through the vault's `GitSync`.

Other server code hooks into ending with two ordered hook lists, each hook an async callable of the
session id, bounded by `end_hook_timeout` and never able to stop the end (a failure or timeout is
logged): `add_before_ended(hook)` runs before `session.ended` is published (a server-side STT
provider flushing its tail, #136) and `add_before_close(hook)` after it is published and before the
vault ends the session (the transcript pipeline's `drain()`, so every `transcript.final` published
before `session.ended` is in `transcript.jsonl` first). The session is still attached to the bus
while both run.

The vault is pulled (`GitSync.sync()`) when it is first opened, before the scan for unended
sessions, and again before every session start; a `conflict` refuses the start
(`VaultSyncConflictError`), while an unreachable remote or refused credentials are only logged
(offline-first). While the app is serving (`startup()` .. `shutdown()`, the app's lifespan) the
`GitSync.run()` loop commits and pushes in the background once the vault is open, and shutdown
flushes whatever is still pending.

One active writer between PCs (ADR-0002): after each of those pulls the vault's active-host record
(`.sa/active.yaml`) is checked, and another PC's unreleased, not stale claim becomes the
`host_warning` the status route shows -- a warning, never a refusal. A session start then claims
the record for this host, commits it and asks for an immediate push; its end releases it before
the end's checkpoint and push.

The derived search index (`VaultIndex`, ADR-0002) is opened at `[vault] index_path` right after
the vault is (after its first pull), in a worker thread; an index that cannot be opened is logged
and left out (`index` stays `None`), never failing the vault. While serving, `VaultIndex.run()`
keeps it current in the background next to the sync loop; after the pull at every session start a
`refresh()` is scheduled in a worker thread (without delaying the start); shutdown stops both and
closes the index.

Ids on the wire are vault slugs: `subject_id` is the subject's slug, `topic_id` the topic's slug
within that subject, and `session_id` the vault's `YYYYMMDD-HHMMSS` session id. Every vault call
runs in a worker thread, and lifecycle changes are serialised by one lock.

A session's stored captures are the `capture.stored` events of its `events.jsonl`
(`stored_captures`): session start and resume report their ids in `received_capture_ids`, and the
capture upload route (`captures.py`) reads them to answer a repeated `capture_id` as a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar

from studentassistant import protocol
from studentassistant.config import VaultSettings
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    CAPTURE_ID_KEY,
    ObserverStateError,
    load_observer_snapshot,
)
from studentassistant.protocol.version import PROTOCOL_VERSION, negotiate, parse_version
from studentassistant.server.auth import Principal
from studentassistant.server.bus import SessionBus
from studentassistant.vault import (
    ActiveHostWarning,
    GitSync,
    NoOpenSessionError,
    Session,
    StoredSubject,
    SyncResult,
    Vault,
    VaultError,
    check_active_host,
    claim_active_host,
    create_subject,
    create_topic,
    end_session,
    list_sessions,
    list_subjects,
    list_topics,
    release_active_host,
    resume_session,
    start_session,
)
from studentassistant.vault.index import VaultIndex, VaultIndexError

SESSION_STARTED = "session.started"
SESSION_RESUMED = "session.resumed"
SESSION_ENDED = "session.ended"
LIFECYCLE_KINDS = frozenset({SESSION_STARTED, SESSION_RESUMED, SESSION_ENDED})

T = TypeVar("T")

TOPIC_ACTIVITY_SINCE = (1, 1)
"""The protocol version that added a topic's `last_session_at_ms` and `pending_count`."""

DEFAULT_SYNC_INTERVAL_SECONDS = 1.0
"""How often the background loop asks `GitSync.run_due()` whether a commit or push is due."""

DEFAULT_INDEX_INTERVAL_SECONDS = 5.0
"""How often the background loop brings the search index up to date (`VaultIndex.run()`)."""

DEFAULT_END_HOOK_TIMEOUT_SECONDS = 10.0
"""How long `end` waits for each end hook before logging it and ending the session anyway."""

EndHook = Callable[[str], Awaitable[object]]
"""An end hook: awaited with the id of the session being ended."""

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """A lifecycle request this backend refuses; the message says why."""


class VaultUnavailableError(LifecycleError):
    """No vault could be opened at the configured path."""


class UnknownSessionError(LifecycleError, LookupError):
    """No topic of the vault lists this session id."""


class SessionConflictError(LifecycleError):
    """The request clashes with the current state (another session active, or already ended)."""


class ActiveSessionExistsError(SessionConflictError):
    """Another session is active or still unended; `session_id` names it."""

    def __init__(self, message: str, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class SessionAlreadyEndedError(SessionConflictError):
    """The session has ended; it cannot be resumed or ended again."""


class VaultSyncConflictError(SessionConflictError):
    """Pulling the vault hit a conflict git cannot merge; `conflicts` lists the paths.

    Nothing is auto-resolved (ADR-0002): the rebase was aborted and the local state kept, and no
    session starts until the student resolves it.
    """

    def __init__(self, message: str, conflicts: tuple[str, ...]) -> None:
        super().__init__(message)
        self.conflicts = conflicts


@dataclass(frozen=True)
class OpenSession:
    """The active session as other server code (the WebSocket gateway) sees it."""

    session_id: str
    subject_id: str
    topic_id: str
    started_at: datetime

    @property
    def started_at_ms(self) -> int:
        return _epoch_ms(self.started_at)


class SessionService:
    """Subjects/topics listing and creation, and the session lifecycle, over one vault.

    Give it an open `vault` (tests: `tmp_vault`), or `vault_settings` to open the configured
    vault lazily on first use (`VaultUnavailableError` while it cannot be opened). `sync` defaults
    to a `GitSync` of that vault with `vault_settings.git`. `sync_interval` is how often the
    background loop (only between `startup()` and `shutdown()`) checks what is due.
    `end_hook_timeout` bounds each end hook (`add_before_ended`, `add_before_close`).
    `index_interval` is how often the background loop updates the search index, which lives at
    `vault_settings.index_path`.
    """

    def __init__(
        self,
        bus: SessionBus,
        *,
        vault: Vault | None = None,
        sync: GitSync | None = None,
        vault_settings: VaultSettings | None = None,
        host: str | None = None,
        sync_interval: float = DEFAULT_SYNC_INTERVAL_SECONDS,
        end_hook_timeout: float = DEFAULT_END_HOOK_TIMEOUT_SECONDS,
        index_interval: float = DEFAULT_INDEX_INTERVAL_SECONDS,
    ) -> None:
        self.bus = bus
        self._vault = vault
        self._sync = sync
        self._settings = vault_settings or VaultSettings()
        self.host = host or socket.gethostname()
        self._lock = asyncio.Lock()
        self._loaded = False
        # Unended sessions by (subject, topic); the attached one is `_active`.
        self._open: dict[tuple[str, str], str] = {}
        self._active: Session | None = None
        self._sync_interval = sync_interval
        self._serving = False
        self._runner: asyncio.Task[None] | None = None
        self._end_hook_timeout = end_hook_timeout
        self._before_ended: list[EndHook] = []
        self._before_close: list[EndHook] = []
        self._index: VaultIndex | None = None
        self._index_interval = index_interval
        self._index_runner: asyncio.Task[None] | None = None
        self._index_refresh: asyncio.Task[None] | None = None
        self._host_warning: ActiveHostWarning | None = None
        if bus.on_append is None:
            bus.on_append = self._note_change

    # -- state ---------------------------------------------------------------------------------

    @property
    def active(self) -> OpenSession | None:
        """The session currently attached to the bus, if any."""
        return None if self._active is None else _open_session(self._active)

    def get_active(self, session_id: str) -> OpenSession | None:
        """The active session when it is `session_id`, else None (unknown, ended, not resumed)."""
        active = self.active
        return active if active is not None and active.session_id == session_id else None

    @property
    def sync(self) -> GitSync | None:
        return self._sync

    async def open_vault(self) -> Vault:
        """The vault, opened (and pulled and scanned) on first use, for the read-only routes.

        Raises:
            VaultUnavailableError: the vault cannot be opened.
        """
        return await self._ready()

    @property
    def host_warning(self) -> ActiveHostWarning | None:
        """Another PC's open claim on the vault, as of the last pull (vault open, session start)."""
        return self._host_warning

    @property
    def sync_running(self) -> bool:
        """Whether the background `GitSync.run()` loop is running."""
        return self._runner is not None and not self._runner.done()

    @property
    def index(self) -> VaultIndex | None:
        """The vault's search index once the vault is open; `None` before, or if it cannot open."""
        return self._index

    @property
    def index_running(self) -> bool:
        """Whether the background `VaultIndex.run()` loop is running."""
        return self._index_runner is not None and not self._index_runner.done()

    async def wait_index_refreshed(self) -> None:
        """Wait for the index refresh a session start scheduled, if one is still running."""
        task = self._pending_refresh()
        if task is not None:
            await asyncio.wait({task})

    # -- end hooks ---------------------------------------------------------------------------

    def add_before_ended(self, hook: EndHook) -> None:
        """Run `hook(session_id)` in `end` before `session.ended` is published.

        For what must still reach the session's logs as events before its end (a server-side STT
        provider's `finish()` tail). Hooks run in the order they were added.
        """
        self._before_ended.append(hook)

    def add_before_close(self, hook: EndHook) -> None:
        """Run `hook(session_id)` in `end` after `session.ended` is published, before `end_session`.

        For consumers that must finish writing what was published before the end (the transcript
        pipeline's `drain()`). Hooks run in the order they were added.
        """
        self._before_close.append(hook)

    async def _run_end_hooks(self, hooks: list[EndHook], session_id: str, stage: str) -> None:
        for hook in hooks:
            try:
                await asyncio.wait_for(hook(session_id), self._end_hook_timeout)
            except TimeoutError:
                logger.error(
                    "end hook %r (%s) of session %s timed out after %.1f s; ending anyway",
                    hook,
                    stage,
                    session_id,
                    self._end_hook_timeout,
                )
            except Exception:
                logger.exception(
                    "end hook %r (%s) of session %s failed; ending anyway", hook, stage, session_id
                )

    # -- serving (the app's lifespan) ----------------------------------------------------------

    async def startup(self) -> None:
        """Start serving: from now on an open vault gets the background commit/push loop.

        The vault itself is still opened lazily, by the first request that needs it.
        """
        self._serving = True
        if self._loaded:
            self._start_runner()

    async def shutdown(self) -> None:
        """Stop the background loops, commit and push what is pending, then close the index.

        The flush and the close run in worker threads; a refresh still running is waited for.
        """
        self._serving = False
        runner, self._runner = self._runner, None
        index_runner, self._index_runner = self._index_runner, None
        for task in (runner, index_runner):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._loaded and self._sync is not None:
            await asyncio.to_thread(self._sync.flush)
        refresh, self._index_refresh = self._pending_refresh(), None
        if refresh is not None:
            await asyncio.wait({refresh})
        index, self._index = self._index, None
        if index is not None:
            # `close()` waits for the index lock; an update still in its worker thread holds it.
            await asyncio.to_thread(index.close)

    def _start_runner(self) -> None:
        if self._serving and self._sync is not None and not self.sync_running:
            self._runner = asyncio.create_task(
                self._sync.run(self._sync_interval), name="vault-git-sync"
            )
        if self._serving and self._index is not None and not self.index_running:
            self._index_runner = asyncio.create_task(
                self._index.run(self._index_interval), name="vault-index"
            )

    # -- subjects and topics -------------------------------------------------------------------

    async def list_subjects(self) -> protocol.SubjectsListResponse:
        vault = await self._ready()
        stored = await asyncio.to_thread(list_subjects, vault)
        return protocol.SubjectsListResponse(subjects=[_subject(s) for s in stored])

    async def create_subject(self, name: str) -> protocol.Subject:
        vault = await self._ready()
        async with self._lock:
            stored = await asyncio.to_thread(create_subject, vault, name)
        self._note_change()
        return _subject(stored)

    async def list_topics(
        self, subject_id: str, *, protocol_version: str = PROTOCOL_VERSION
    ) -> protocol.TopicsListResponse:
        """The subject's topics, shaped for a client speaking `protocol_version`.

        Peers speak the lower MINOR, and a client refuses unknown fields, so a topic carries
        `last_session_at_ms` and `pending_count` (added in 1.1) only for a 1.1+ client.
        """
        vault = await self._ready()
        stored = await asyncio.to_thread(list_topics, vault, subject_id)
        if _speaks_at_least(protocol_version, TOPIC_ACTIVITY_SINCE):
            activity = await asyncio.to_thread(
                lambda: [_topic_activity(vault, subject_id, t.slug) for t in stored]
            )
        else:
            activity = [(None, None)] * len(stored)
        return protocol.TopicsListResponse(
            subject_id=subject_id,
            topics=[
                self._topic(subject_id, t.slug, t.topic.title, last, pending)
                for t, (last, pending) in zip(stored, activity, strict=True)
            ],
        )

    async def create_topic(self, subject_id: str, name: str) -> protocol.Topic:
        vault = await self._ready()
        async with self._lock:
            stored = await asyncio.to_thread(create_topic, vault, subject_id, name)
        self._note_change()
        return self._topic(subject_id, stored.slug, stored.topic.title)

    # -- sessions ------------------------------------------------------------------------------

    async def start(
        self,
        subject_id: str,
        topic_id: str,
        *,
        client_time_ms: int,
        principal: Principal | None = None,
    ) -> protocol.Session:
        """Start a session of one topic and publish `session.started`.

        The vault is pulled first; see `_pull`.

        Raises:
            ActiveSessionExistsError: a session is active or still unended.
            VaultSyncConflictError: pulling the vault hit a conflict; nothing was started.
            SubjectNotFoundError, TopicNotFoundError: no such subject or topic.
        """
        vault = await self._ready()
        async with self._lock:
            if self._open:
                (subject, topic), existing = next(iter(self._open.items()))
                raise ActiveSessionExistsError(
                    f"session {existing} of topic {subject}/{topic} is still open: resume or end"
                    " it before starting another",
                    existing,
                )
            result = await self._pull("session start")
            if result is not None and result.outcome == "conflict":
                paths = ", ".join(result.conflicts) or "(git named no path)"
                raise VaultSyncConflictError(
                    f"the vault could not be synced: {result.message}; conflicting paths: {paths}."
                    " Resolve the conflict in the vault before starting a session",
                    result.conflicts,
                )
            self._schedule_index_refresh()
            await self._check_host(vault)
            session = await asyncio.to_thread(
                start_session, vault, subject_id, topic_id, self.host, PROTOCOL_VERSION
            )
            await asyncio.to_thread(
                claim_active_host, vault, self.host, session.id, subject_id, topic_id
            )
            self._note_change()
            self._open[(subject_id, topic_id)] = session.id
            self._attach(session)
            await self.bus.publish(
                session.id,
                SESSION_STARTED,
                "user",
                {
                    "subject_id": subject_id,
                    "topic_id": topic_id,
                    "client_time_ms": client_time_ms,
                    "device_id": _device(principal),
                },
            )
            sync = self._sync
            if sync is not None:
                # The claim is committed now and pushed by the background loop right away, so the
                # other PCs see it without the start waiting for the network.
                await asyncio.to_thread(
                    sync.checkpoint, f"sesión {session.id} iniciada en {self.host}"
                )
                sync.request_push()
            return await _wire_session(session)

    async def resume(
        self, session_id: str, *, principal: Principal | None = None
    ) -> protocol.Session:
        """Make an unended session active again (or confirm it is) and publish `session.resumed`.

        Raises:
            UnknownSessionError: no topic lists the session.
            SessionAlreadyEndedError: the session has ended.
            ActiveSessionExistsError: another session is active.
        """
        await self._ready()
        async with self._lock:
            if self._active is not None and self._active.id != session_id:
                raise ActiveSessionExistsError(
                    f"session {self._active.id} is active: end it before resuming {session_id}",
                    self._active.id,
                )
            session = await self._load_open(session_id)
            self._attach(session)
            await self.bus.publish(
                session.id, SESSION_RESUMED, "user", {"device_id": _device(principal)}
            )
            return await _wire_session(session)

    async def end(
        self,
        session_id: str,
        *,
        client_time_ms: int,
        reason: Literal["button", "command"],
        principal: Principal | None = None,
    ) -> protocol.SessionEndResponse:
        """Publish `session.ended`, end the session, then checkpoint and push the vault.

        In order: the `add_before_ended` hooks, `session.ended` is published, the
        `add_before_close` hooks, `end_session`, the bus detach, the checkpoint and push.

        A failed commit or push is recorded in the sync status, never raised: the session has
        ended either way and the next sync retries.

        Raises:
            UnknownSessionError, SessionAlreadyEndedError: as for `resume`.
        """
        await self._ready()
        async with self._lock:
            session = await self._load_open(session_id)
            # A session left unended by an earlier run is attached just long enough to log its end.
            attached_here = not self.bus.is_attached(session.id)
            if attached_here:
                self.bus.attach(session)
            try:
                await self._run_end_hooks(self._before_ended, session.id, "before ended")
                await self.bus.publish(
                    session.id,
                    SESSION_ENDED,
                    "user",
                    {
                        "client_time_ms": client_time_ms,
                        "reason": reason,
                        "device_id": _device(principal),
                    },
                )
                await self._run_end_hooks(self._before_close, session.id, "before close")
                meta = await asyncio.to_thread(end_session, session)
                await asyncio.to_thread(release_active_host, session.vault, self.host, session.id)
            except BaseException:
                if attached_here:
                    self.bus.detach(session.id)
                raise
            self.bus.detach(session.id)
            self._note_change()
            if self._active is not None and self._active.id == session.id:
                self._active = None
            self._open.pop((session.subject_slug, session.topic_slug), None)
            sync = self._sync
            if sync is not None:
                await asyncio.to_thread(sync.checkpoint, f"sesión {session.id} terminada")
                await asyncio.to_thread(sync.push_now)
            assert meta.ended_at is not None
            return protocol.SessionEndResponse(
                session_id=session.id, status="ended", ended_at_ms=_epoch_ms(meta.ended_at)
            )

    # -- the active session, for the other routes ----------------------------------------------

    async def require_active(self, session_id: str) -> Session:
        """The vault handle of the active session when it is `session_id`.

        Raises:
            VaultUnavailableError: the vault cannot be opened.
            UnknownSessionError: no topic lists the session.
            SessionAlreadyEndedError: the session has ended.
            SessionConflictError: the session is unended but not the active one (not resumed).
        """
        vault = await self._ready()
        active = self._active
        if active is not None and active.id == session_id:
            return active
        if session_id in self._open.values():
            raise SessionConflictError(
                f"la sesión {session_id} no está activa: reanúdala antes de enviarle nada"
            )
        if await asyncio.to_thread(self._is_listed, vault, session_id):
            raise SessionAlreadyEndedError(f"la sesión {session_id} ya ha terminado")
        raise UnknownSessionError(f"no existe la sesión {session_id}")

    def note_change(self) -> None:
        """Tell the vault's `GitSync` a vault file was written (a no-op before the vault opens)."""
        self._note_change()

    # -- internals -----------------------------------------------------------------------------

    def _attach(self, session: Session) -> None:
        self._active = session
        self.bus.attach(session)

    async def _load_open(self, session_id: str) -> Session:
        """The handle of an unended session: the attached one, or reopened from the vault."""
        if self._active is not None and self._active.id == session_id:
            return self._active
        vault = await self._ready()
        where = next((key for key, value in self._open.items() if value == session_id), None)
        if where is None:
            if await asyncio.to_thread(self._is_listed, vault, session_id):
                raise SessionAlreadyEndedError(f"session {session_id} has ended")
            raise UnknownSessionError(f"no topic has a session {session_id}")
        subject_id, topic_id = where
        session = await asyncio.to_thread(resume_session, vault, subject_id, topic_id)
        if session.id != session_id:
            raise SessionConflictError(
                f"session {session_id} is not the open session of {subject_id}/{topic_id}"
            )
        return session

    @staticmethod
    def _is_listed(vault: Vault, session_id: str) -> bool:
        for subject in list_subjects(vault):
            for topic in list_topics(vault, subject.slug):
                if session_id in topic.topic.sessions:
                    return True
        return False

    async def _ready(self) -> Vault:
        """The vault, opened and scanned for unended sessions on first use."""
        if self._loaded and self._vault is not None:
            return self._vault
        async with self._lock:
            if not self._loaded:
                vault = self._vault
                if vault is None:
                    vault = await self._call(Vault.open, self._settings.path)
                if self._sync is None:
                    self._sync = GitSync(vault, self._settings.git)
                # Pull before the scan, so sessions another PC left open are seen. A conflict is
                # only logged here: the next session start pulls again and refuses on it.
                await self._pull("vault open")
                await self._check_host(vault)
                self._open = await asyncio.to_thread(_scan_open_sessions, vault)
                self._index = await self._open_index(vault)
                self._vault = vault
                self._loaded = True
                self._start_runner()
        assert self._vault is not None
        return self._vault

    @staticmethod
    async def _call(function: Callable[..., T], *args: object) -> T:
        try:
            return await asyncio.to_thread(function, *args)
        except VaultError as error:
            raise VaultUnavailableError(f"the vault cannot be opened: {error}") from error

    async def _pull(self, when: str) -> SyncResult | None:
        """`GitSync.sync()` in a worker thread; every outcome but `ok` is logged, none raised."""
        sync = self._sync
        if sync is None:
            return None
        result = await asyncio.to_thread(sync.sync)
        if result.outcome == "conflict":
            logger.error(
                "vault sync at %s: conflict in %s: %s",
                when,
                ", ".join(result.conflicts),
                result.message,
            )
        elif not result.ok:
            # offline, auth, error: work goes on locally and a later sync or push catches up.
            logger.warning("vault sync at %s: %s: %s", when, result.outcome, result.message)
        return result

    async def _open_index(self, vault: Vault) -> VaultIndex | None:
        """`VaultIndex.open` in a worker thread; a failure is logged and leaves the index out."""
        path = self._settings.index_path
        try:
            return await asyncio.to_thread(VaultIndex.open, vault, path)
        except (VaultIndexError, sqlite3.Error, OSError) as error:
            logger.error("search index at %s cannot be opened; search is off: %s", path, error)
            return None

    def _schedule_index_refresh(self) -> None:
        """Refresh the index in a worker thread after a pull, unless a refresh is still running."""
        index = self._index
        if index is None or self._pending_refresh() is not None:
            return
        self._index_refresh = asyncio.create_task(_refresh(index), name="vault-index-refresh")

    def _pending_refresh(self) -> asyncio.Task[None] | None:
        """The scheduled refresh while it runs on this event loop.

        A request served outside the app's lifespan (a `TestClient` used without `with`) runs on
        a loop of its own that is gone afterwards; its task is never waited for.
        """
        task = self._index_refresh
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            return None
        return task

    async def _check_host(self, vault: Vault) -> None:
        """Read the active-host record just pulled into `host_warning`; logs a warning it finds."""
        stale_after = (
            self._sync.settings if self._sync is not None else self._settings.git
        ).active_host_stale_seconds
        warning = await asyncio.to_thread(check_active_host, vault, self.host, stale_after)
        if warning is not None:
            logger.warning("vault active host: %s", warning.message)
        self._host_warning = warning

    def _note_change(self) -> None:
        if self._sync is not None:
            self._sync.note_change()

    def _topic(
        self,
        subject_id: str,
        topic_id: str,
        title: str,
        last_session_at_ms: int | None = None,
        pending_count: int | None = None,
    ) -> protocol.Topic:
        return protocol.Topic(
            topic_id=topic_id,
            subject_id=subject_id,
            name=title,
            open_session_id=self._open.get((subject_id, topic_id)),
            last_session_at_ms=last_session_at_ms,
            pending_count=pending_count,
        )


async def _refresh(index: VaultIndex) -> None:
    try:
        await asyncio.to_thread(index.refresh)
    except (VaultError, sqlite3.Error, OSError) as error:
        logger.warning("search index refresh failed: %s", error)


def _speaks_at_least(client: str, since: tuple[int, int]) -> bool:
    """Whether the version negotiated with a client speaking `client` is at least `since`."""
    try:
        return parse_version(negotiate(client)) >= since
    except ValueError:
        return False


def _topic_activity(vault: Vault, subject_id: str, topic_id: str) -> tuple[int | None, int | None]:
    """The topic's latest session start (epoch ms) and open pending count, `None` when unknown.

    Each is read on its own through the vault's and the observer's public functions; one that
    cannot be read is logged and left out, so a damaged session never hides the topic list.
    """
    last: int | None = None
    try:
        sessions = list_sessions(vault, subject_id, topic_id)
    except VaultError as error:
        logger.warning("topic %s/%s: sessions unreadable: %s", subject_id, topic_id, error)
    else:
        if sessions:
            last = _epoch_ms(max(meta.started_at for meta in sessions))
    pending: int | None = None
    try:
        snapshot = load_observer_snapshot(vault, subject_id, topic_id, write_back=False)
    except (VaultError, ObserverStateError) as error:
        logger.warning("topic %s/%s: observer state unreadable: %s", subject_id, topic_id, error)
    else:
        pending = len(snapshot.state.open_pending())
    return last, pending


def _scan_open_sessions(vault: Vault) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for subject in list_subjects(vault):
        for topic in list_topics(vault, subject.slug):
            if not topic.topic.sessions:
                continue
            try:
                session = resume_session(vault, subject.slug, topic.slug)
            except NoOpenSessionError:
                continue
            found[(subject.slug, topic.slug)] = session.id
    return found


def _subject(stored: StoredSubject) -> protocol.Subject:
    return protocol.Subject(subject_id=stored.slug, name=stored.subject.name)


def _open_session(session: Session) -> OpenSession:
    return OpenSession(
        session_id=session.id,
        subject_id=session.subject_slug,
        topic_id=session.topic_slug,
        started_at=session.meta.started_at,
    )


def stored_captures(session: Session) -> dict[str, Mapping[str, Any]]:
    """The captures stored for a session: `capture_id` -> payload of its `capture.stored` event.

    Read from the session's `events.jsonl`, so it survives a backend restart; in log order, and a
    repeated id keeps its first event. Blocking file I/O: call it from a worker thread.
    """
    found: dict[str, Mapping[str, Any]] = {}
    for event in session.read_events():
        if event.kind != CAPTURE_EVENT_KIND:
            continue
        capture_id = event.payload.get(CAPTURE_ID_KEY)
        if isinstance(capture_id, str) and capture_id not in found:
            found[capture_id] = event.payload
    return found


async def _wire_session(session: Session) -> protocol.Session:
    captures = await asyncio.to_thread(stored_captures, session)
    return protocol.Session(
        session_id=session.id,
        subject_id=session.subject_slug,
        topic_id=session.topic_slug,
        status="active",
        started_at_ms=_epoch_ms(session.meta.started_at),
        ws_path=f"/ws/sessions/{session.id}",
        protocol_version=PROTOCOL_VERSION,
        received_capture_ids=list(captures),
    )


def _device(principal: Principal | None) -> str | None:
    return None if principal is None else principal.device_id


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


__all__ = [
    "DEFAULT_END_HOOK_TIMEOUT_SECONDS",
    "DEFAULT_INDEX_INTERVAL_SECONDS",
    "DEFAULT_SYNC_INTERVAL_SECONDS",
    "LIFECYCLE_KINDS",
    "SESSION_ENDED",
    "SESSION_RESUMED",
    "SESSION_STARTED",
    "ActiveSessionExistsError",
    "EndHook",
    "LifecycleError",
    "OpenSession",
    "SessionAlreadyEndedError",
    "SessionConflictError",
    "SessionService",
    "UnknownSessionError",
    "VaultSyncConflictError",
    "VaultUnavailableError",
    "stored_captures",
]
