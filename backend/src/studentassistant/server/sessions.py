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

The vault is pulled (`GitSync.sync()`) when it is first opened, before the scan for unended
sessions, and again before every session start; a `conflict` refuses the start
(`VaultSyncConflictError`), while an unreachable remote or refused credentials are only logged
(offline-first). While the app is serving (`startup()` .. `shutdown()`, the app's lifespan) the
`GitSync.run()` loop commits and pushes in the background once the vault is open, and shutdown
flushes whatever is still pending.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar

from studentassistant import protocol
from studentassistant.config import VaultSettings
from studentassistant.observer import CAPTURE_EVENT_KIND, CAPTURE_ID_KEY
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.auth import Principal
from studentassistant.server.bus import SessionBus
from studentassistant.vault import (
    GitSync,
    NoOpenSessionError,
    Session,
    StoredSubject,
    SyncResult,
    Vault,
    VaultError,
    create_subject,
    create_topic,
    end_session,
    list_subjects,
    list_topics,
    resume_session,
    start_session,
)

SESSION_STARTED = "session.started"
SESSION_RESUMED = "session.resumed"
SESSION_ENDED = "session.ended"
LIFECYCLE_KINDS = frozenset({SESSION_STARTED, SESSION_RESUMED, SESSION_ENDED})

T = TypeVar("T")

DEFAULT_SYNC_INTERVAL_SECONDS = 1.0
"""How often the background loop asks `GitSync.run_due()` whether a commit or push is due."""

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

    @property
    def sync_running(self) -> bool:
        """Whether the background `GitSync.run()` loop is running."""
        return self._runner is not None and not self._runner.done()

    # -- serving (the app's lifespan) ----------------------------------------------------------

    async def startup(self) -> None:
        """Start serving: from now on an open vault gets the background commit/push loop.

        The vault itself is still opened lazily, by the first request that needs it.
        """
        self._serving = True
        if self._loaded:
            self._start_runner()

    async def shutdown(self) -> None:
        """Stop the background loop, then commit and push what is pending (in a worker thread)."""
        self._serving = False
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        if self._loaded and self._sync is not None:
            await asyncio.to_thread(self._sync.flush)

    def _start_runner(self) -> None:
        if self._serving and self._sync is not None and not self.sync_running:
            self._runner = asyncio.create_task(
                self._sync.run(self._sync_interval), name="vault-git-sync"
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

    async def list_topics(self, subject_id: str) -> protocol.TopicsListResponse:
        vault = await self._ready()
        stored = await asyncio.to_thread(list_topics, vault, subject_id)
        return protocol.TopicsListResponse(
            subject_id=subject_id,
            topics=[self._topic(subject_id, t.slug, t.topic.title) for t in stored],
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
            session = await asyncio.to_thread(
                start_session, vault, subject_id, topic_id, self.host, PROTOCOL_VERSION
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
                meta = await asyncio.to_thread(end_session, session)
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
                self._open = await asyncio.to_thread(_scan_open_sessions, vault)
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

    def _note_change(self) -> None:
        if self._sync is not None:
            self._sync.note_change()

    def _topic(self, subject_id: str, topic_id: str, title: str) -> protocol.Topic:
        return protocol.Topic(
            topic_id=topic_id,
            subject_id=subject_id,
            name=title,
            open_session_id=self._open.get((subject_id, topic_id)),
        )


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
    "DEFAULT_SYNC_INTERVAL_SECONDS",
    "LIFECYCLE_KINDS",
    "SESSION_ENDED",
    "SESSION_RESUMED",
    "SESSION_STARTED",
    "ActiveSessionExistsError",
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
