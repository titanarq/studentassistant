"""The session lifecycle service: subjects, topics and the `active` -> `ended` session machine.

A session belongs to exactly one topic of one subject of ONE USER, all three fixed when it starts
(ADR-0003, epic #544); switching topic means ending the session and starting another. The service
holds the vault's root handle -- the repository, where git, the locks and `.sa/active.yaml` live --
and every subject, topic and session it reads or writes goes through `vault.for_user(user_id)`, so
nothing a lifecycle call does can land in another student's folder. Opening the vault therefore
scans EVERY user's topics for unended sessions: one backend still has at most one active session
at a time, whichever student it is, and a session another PC or another student left unended is
found whoever asks next.

Which user a call acts for is its first argument. A call that names none (`user_id is None`, what
the routes still do until they resolve the active user of the request, #550) acts for the vault's
single user, the same fallback protocol 1.8 gives a request with neither `X-SA-User` nor `sa_user`;
with any other number of users there is nobody to assume and `NoUserError` says so.

Starting a session while one is still unended is refused, and so is resuming one while another is
active. The refusal names the session only to the student it is theirs: another user's start or
resume gets `OtherUserSessionOpenError`, whose Spanish message says somebody else is capturing on
this computer and carries no session id, because another user's session is never revealed (#550).
A session id asked for by a user it is not theirs is unknown, never theirs: `require_active`,
`resume`, `is_known` and `open_session_of` look under one user's folder only. Resuming continues
the session's logs' `seq` where they stopped.

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
flushes whatever is still pending. `add_on_open(hook)` hooks are called with the vault once it is
first opened, pulled and scanned (the page transcriber's server-start catch-up, #181); the
handle they get is the root one, so a hook that works over one student's content narrows it with
`for_user` itself (#550).

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
within that subject, and `session_id` the vault's `YYYYMMDD-HHMMSS` session id. They are relative
to the user's folder, as every vault-relative id is (epic #544), so two students may have a
`fisica/cinematica` and a session id is only unique inside one of them: which is why every call
here is told whose it is. Every vault call runs in a worker thread, and lifecycle changes are
serialised by one lock.

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
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, TypeVar

from studentassistant import protocol
from studentassistant.config import VaultSettings
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    CAPTURE_ID_KEY,
    ObserverStateError,
    digest_excerpt,
    load_observer_snapshot,
    topic_digest,
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
    UserNotFoundError,
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
    user_ids,
)
from studentassistant.vault.index import VaultIndex, VaultIndexError

SESSION_STARTED = "session.started"
SESSION_RESUMED = "session.resumed"
SESSION_ENDED = "session.ended"
LIFECYCLE_KINDS = frozenset({SESSION_STARTED, SESSION_RESUMED, SESSION_ENDED})

T = TypeVar("T")

TOPIC_ACTIVITY_SINCE = (1, 1)
"""The protocol version that added a topic's `last_session_at_ms` and `pending_count`."""

TOPIC_DIGEST_SINCE = (1, 3)
"""The protocol version that added a topic's `digest_excerpt`."""

DEFAULT_SYNC_INTERVAL_SECONDS = 1.0
"""How often the background loop asks `GitSync.run_due()` whether a commit or push is due."""

DEFAULT_INDEX_INTERVAL_SECONDS = 5.0
"""How often the background loop brings the search index up to date (`VaultIndex.run()`)."""

DEFAULT_END_HOOK_TIMEOUT_SECONDS = 10.0
"""How long `end` waits for each end hook before logging it and ending the session anyway."""

OTHER_USER_SESSION_OPEN_DETAIL = (
    "Otro usuario tiene una sesión de captura abierta en este ordenador"
)
"""What a start or resume by another user is told while a session is unended (#550).

Spanish, and it names neither the user nor the session: the one active session of a backend
belongs to somebody else, and that is all the student asking is told.
"""

EndHook = Callable[[str], Awaitable[object]]
"""An end hook: awaited with the id of the session being ended."""

EndReason = Literal["button", "command", "idle"]
"""Why a session ended: `button`/`command` come from a client's end request; `idle` is the
backend's own end of a capture session no client was sending to (`capture_liveness.py`, #425)."""

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """A lifecycle request this backend refuses; the message says why."""


class VaultUnavailableError(LifecycleError):
    """No vault could be opened at the configured path."""


class UnknownSessionError(LifecycleError, LookupError):
    """No topic of the vault lists this session id."""


class UnknownUserError(LifecycleError, LookupError):
    """The vault has no such user, so nothing can be read or written as them."""

    def __init__(self, user_id: str) -> None:
        super().__init__(f"this vault has no user {user_id!r}")
        self.user_id = user_id


class NoUserError(LifecycleError):
    """The call named no user and the vault does not hold exactly one, so none can be assumed.

    The routes of #550 resolve the active user of a request and name it, which is why a refusal
    here is a defect rather than something a client is told: it means a caller left the fallback
    in place over a vault several students share.
    """


class SessionConflictError(LifecycleError):
    """The request clashes with the current state (another session active, or already ended)."""


class ActiveSessionExistsError(SessionConflictError):
    """Another session is active or still unended; `session_id` names it."""

    def __init__(self, message: str, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class OtherUserSessionOpenError(SessionConflictError):
    """The one active session of this backend is another user's (#550).

    Deliberately not an `ActiveSessionExistsError`: it carries no session id, because the route
    answers it with `409 session_open` and NO `X-Open-Session-Id` header, and its message is the
    Spanish `OTHER_USER_SESSION_OPEN_DETAIL` the student reads.
    """

    def __init__(self, message: str = OTHER_USER_SESSION_OPEN_DETAIL) -> None:
        super().__init__(message)


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
    """The active session as other server code (the WebSocket gateway) sees it.

    `user_id` is the student the session belongs to: every consumer that reads or writes its
    content goes through `vault.for_user(user_id)` and `sync.for_user(user_id)` (#550).
    """

    session_id: str
    user_id: str
    subject_id: str
    topic_id: str
    started_at: datetime

    @property
    def started_at_ms(self) -> int:
        return _epoch_ms(self.started_at)


class SessionService:
    """Subjects/topics listing and creation, and the session lifecycle, over one vault.

    Give it an open `vault` (tests: `tmp_vault`), or `vault_settings` to open the configured
    vault lazily on first use (`VaultUnavailableError` while it cannot be opened). Either way the
    handle the service works on is the ROOT one -- the repository's, from which `for_user` narrows
    to a student's folder -- so a user handle it is given is widened back to its root first, and
    `open_vault()` gives the root to the routes that still read the whole vault (#551). `sync`
    defaults to a `GitSync` of that vault with `vault_settings.git`: one sync per repository, whose
    `for_user` view is what scopes a student's paths and notes tags. `sync_interval` is how often
    the background loop (only between `startup()` and `shutdown()`) checks what is due.
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
        # The root handle of the vault (the repository), and the user handles derived from it.
        self._root: Vault | None = None
        self._users: dict[str, Vault] = {}
        # Unended sessions by (user, subject, topic); the attached one is `_active`.
        self._open: dict[tuple[str, str, str], str] = {}
        self._active: Session | None = None
        self._sync_interval = sync_interval
        self._serving = False
        self._runner: asyncio.Task[None] | None = None
        self._end_hook_timeout = end_hook_timeout
        self._before_ended: list[EndHook] = []
        self._before_close: list[EndHook] = []
        self._on_open: list[Callable[[Vault], object]] = []
        self._on_attached: list[Callable[[str], object]] = []
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
        """The session currently attached to the bus, if any, whichever user's it is."""
        return None if self._active is None else _open_session(self._active)

    async def get_active(self, user_id: str | None, session_id: str) -> OpenSession | None:
        """`user_id`'s active session when it is `session_id`, else None.

        None covers every case a caller has nothing to do about: unknown, ended, not resumed, and
        -- what must not be told apart from them -- a session that is another user's.

        Raises:
            VaultUnavailableError: the vault cannot be opened.
            NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, _ = await self._user_scope(user_id)
        active = self.active
        if active is None or active.session_id != session_id or active.user_id != wanted:
            return None
        return active

    @property
    def sync(self) -> GitSync | None:
        return self._sync

    async def open_vault(self) -> Vault:
        """The vault's ROOT handle, opened (and pulled and scanned) on first use.

        The routes that are not user-scoped yet read the whole repository through it (#551), and
        `server.user_scope` narrows it to the active user with `for_user`; a lifecycle call of this
        service never uses it but the user handle `_user_scope` derives from it.

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

    def add_on_open(self, hook: Callable[[Vault], object]) -> None:
        """Call `hook(vault)` once the vault is open (pulled and scanned), on the event loop.

        For background work over the whole vault at server start (the page transcriber's
        catch-up, #181). It must not block: schedule a task. A failure is logged.
        """
        self._on_open.append(hook)

    def add_on_attached(self, hook: Callable[[str], object]) -> None:
        """Call `hook(session_id)` whenever a session becomes (or is confirmed) the active one.

        Called on `start` and `resume`, right before `session.started` / `session.resumed` is
        published, on the event loop and under the lifecycle lock: it must not block nor call back
        into the service. The capture liveness watchdog starts a session's grace period here
        (#425). A failure is logged.
        """
        self._on_attached.append(hook)

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

    async def list_subjects(self, user_id: str | None) -> protocol.SubjectsListResponse:
        """The subjects of `user_id`'s folder, and nobody else's.

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        _, vault = await self._user_scope(user_id)
        stored = await asyncio.to_thread(list_subjects, vault)
        return protocol.SubjectsListResponse(subjects=[_subject(s) for s in stored])

    async def create_subject(self, user_id: str | None, name: str) -> protocol.Subject:
        """A new subject under `user_id`'s folder.

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        _, vault = await self._user_scope(user_id)
        async with self._lock:
            stored = await asyncio.to_thread(create_subject, vault, name)
        self._note_change()
        return _subject(stored)

    async def list_topics(
        self,
        user_id: str | None,
        subject_id: str,
        *,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> protocol.TopicsListResponse:
        """The subject's topics of `user_id`, shaped for a client speaking `protocol_version`.

        Peers speak the lower MINOR, and a client refuses unknown fields, so a topic carries
        `last_session_at_ms` and `pending_count` (added in 1.1) only for a 1.1+ client and
        `digest_excerpt` (added in 1.3) only for a 1.3+ client. A topic's `open_session_id` is
        the unended session of that user's topic, so a student never sees another's.

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, vault = await self._user_scope(user_id)
        stored = await asyncio.to_thread(list_topics, vault, subject_id)
        if _speaks_at_least(protocol_version, TOPIC_ACTIVITY_SINCE):
            activity = await asyncio.to_thread(
                lambda: [_topic_activity(vault, subject_id, t.slug) for t in stored]
            )
        else:
            activity = [(None, None)] * len(stored)
        if _speaks_at_least(protocol_version, TOPIC_DIGEST_SINCE):
            excerpts = await asyncio.to_thread(
                lambda: [_topic_excerpt(vault, subject_id, t.slug) for t in stored]
            )
        else:
            excerpts = [None] * len(stored)
        return protocol.TopicsListResponse(
            subject_id=subject_id,
            topics=[
                self._topic(wanted, subject_id, t.slug, t.topic.title, last, pending, excerpt)
                for t, (last, pending), excerpt in zip(stored, activity, excerpts, strict=True)
            ],
        )

    async def create_topic(self, user_id: str | None, subject_id: str, name: str) -> protocol.Topic:
        """A new topic of `user_id`'s subject.

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, vault = await self._user_scope(user_id)
        async with self._lock:
            stored = await asyncio.to_thread(create_topic, vault, subject_id, name)
        self._note_change()
        return self._topic(wanted, subject_id, stored.slug, stored.topic.title)

    # -- sessions ------------------------------------------------------------------------------

    async def start(
        self,
        user_id: str | None,
        subject_id: str,
        topic_id: str,
        *,
        client_time_ms: int,
        principal: Principal | None = None,
    ) -> protocol.Session:
        """Start a session of one of `user_id`'s topics and publish `session.started`.

        The vault is pulled first; see `_pull`.

        Raises:
            ActiveSessionExistsError: a session of this same user is active or still unended.
            OtherUserSessionOpenError: the one unended session of this backend is another user's.
            VaultSyncConflictError: pulling the vault hit a conflict; nothing was started.
            SubjectNotFoundError, TopicNotFoundError: no such subject or topic of this user.
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        root = await self._ready()
        wanted, vault = await self._user_scope(user_id)
        async with self._lock:
            if self._open:
                (owner, subject, topic), existing = self._one_open(wanted)
                if owner != wanted:
                    # The session is somebody else's: neither its id nor whose it is is told.
                    raise OtherUserSessionOpenError()
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
            await self._check_host(root)
            session = await asyncio.to_thread(
                start_session, vault, subject_id, topic_id, self.host, PROTOCOL_VERSION
            )
            await asyncio.to_thread(
                claim_active_host,
                vault,
                self.host,
                session.id,
                subject_id,
                topic_id,
                user_id=wanted,
            )
            self._note_change()
            self._open[_key(session)] = session.id
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
        self, user_id: str | None, session_id: str, *, principal: Principal | None = None
    ) -> protocol.Session:
        """Make one of `user_id`'s unended sessions active again and publish `session.resumed`.

        Raises:
            UnknownSessionError: no topic of this user lists the session -- which is also what a
                session of another user is, since it is never revealed (#550).
            SessionAlreadyEndedError: the session has ended.
            ActiveSessionExistsError: another session of this same user is active.
            OtherUserSessionOpenError: the active session is another user's.
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, _ = await self._user_scope(user_id)
        async with self._lock:
            if self._active is not None and self._active.id != session_id:
                if _user_of(self._active) != wanted:
                    raise OtherUserSessionOpenError()
                raise ActiveSessionExistsError(
                    f"session {self._active.id} is active: end it before resuming {session_id}",
                    self._active.id,
                )
            session = await self._load_open(session_id, of_user=wanted)
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
        reason: EndReason,
        principal: Principal | None = None,
        idle_seconds: float | None = None,
    ) -> protocol.SessionEndResponse:
        """Publish `session.ended`, end the session, then checkpoint and push the vault.

        The session id names the session exactly, and the handle the end writes through is the one
        of the user the session was opened for, so no user is asked for here: the routes check
        whose it is before they call (`require_active`, `get_active`).

        `reason` `idle` is the backend's own end (`capture_liveness.py`); its `idle_seconds`, how
        long no capture client was sending, is added to the `session.ended` payload.

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
                payload: dict[str, Any] = {
                    "client_time_ms": client_time_ms,
                    "reason": reason,
                    "device_id": _device(principal),
                }
                if idle_seconds is not None:
                    payload["idle_seconds"] = round(idle_seconds, 3)
                await self.bus.publish(session.id, SESSION_ENDED, "user", payload)
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
            self._open.pop(_key(session), None)
            sync = self._sync
            if sync is not None:
                await asyncio.to_thread(sync.checkpoint, f"sesión {session.id} terminada")
                await asyncio.to_thread(sync.push_now)
            assert meta.ended_at is not None
            return protocol.SessionEndResponse(
                session_id=session.id, status="ended", ended_at_ms=_epoch_ms(meta.ended_at)
            )

    async def open_session_of(
        self, user_id: str | None, subject_id: str, topic_id: str
    ) -> str | None:
        """The id of `user_id`'s topic's unended session (active or left open), None if it has none.

        A topic of another user with the same slugs is a different topic, and its session is never
        the answer.

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, _ = await self._user_scope(user_id)
        return self._open.get((wanted, subject_id, topic_id))

    # -- the active session, for the other routes ----------------------------------------------

    async def require_active(self, user_id: str | None, session_id: str) -> Session:
        """The vault handle of `user_id`'s active session when it is `session_id`.

        The handle carries the user's vault, so what the caller writes through it lands in that
        student's folder and nowhere else.

        Raises:
            UnknownSessionError: no topic of this user lists the session -- which is also the
                answer for a session that is another user's, whose id is never confirmed (#550).
            SessionAlreadyEndedError: the session has ended.
            SessionConflictError: the session is unended but not the active one (not resumed).
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, _ = await self._user_scope(user_id)
        active = self._active
        if active is not None and active.id == session_id:
            if _user_of(active) != wanted:
                raise UnknownSessionError(f"no existe la sesión {session_id}")
            return active
        if session_id in self._open_of(wanted):
            raise SessionConflictError(
                f"la sesión {session_id} no está activa: reanúdala antes de enviarle nada"
            )
        if await self._listed(wanted, session_id):
            raise SessionAlreadyEndedError(f"la sesión {session_id} ya ha terminado")
        raise UnknownSessionError(f"no existe la sesión {session_id}")

    async def is_known(self, user_id: str | None, session_id: str) -> bool:
        """Whether some topic of `user_id` lists `session_id` (active, unended or ended).

        Raises:
            VaultUnavailableError, NoUserError, UnknownUserError: as `_user_scope`.
        """
        wanted, _ = await self._user_scope(user_id)
        if self._active is not None and self._active.id == session_id:
            return _user_of(self._active) == wanted
        if session_id in self._open_of(wanted):
            return True
        return await self._listed(wanted, session_id)

    def note_change(self) -> None:
        """Tell the vault's `GitSync` a vault file was written (a no-op before the vault opens)."""
        self._note_change()

    # -- internals -----------------------------------------------------------------------------

    def _attach(self, session: Session) -> None:
        self._active = session
        self.bus.attach(session)
        for hook in self._on_attached:
            try:
                hook(session.id)
            except Exception:
                logger.exception("session attached hook %r failed", hook)

    async def _load_open(self, session_id: str, *, of_user: str | None = None) -> Session:
        """The handle of an unended session: the attached one, or reopened from the vault.

        `of_user` restricts the search to one student's folder, and a session that is somebody
        else's is then unknown rather than theirs (#550). `end` leaves it out: the session it ends
        is the one the route has already asked about as that user, and the id names it exactly.
        """
        if self._active is not None and self._active.id == session_id:
            if of_user is None or _user_of(self._active) == of_user:
                return self._active
            raise UnknownSessionError(f"no topic has a session {session_id}")
        where = next((key for key, value in self._open.items() if value == session_id), None)
        if where is not None and of_user is not None and where[0] != of_user:
            raise UnknownSessionError(f"no topic has a session {session_id}")
        if where is None:
            if await self._listed(of_user, session_id):
                raise SessionAlreadyEndedError(f"session {session_id} has ended")
            raise UnknownSessionError(f"no topic has a session {session_id}")
        user_id, subject_id, topic_id = where
        session = await asyncio.to_thread(
            resume_session, self._users[user_id], subject_id, topic_id
        )
        if session.id != session_id:
            raise SessionConflictError(
                f"session {session_id} is not the open session of {subject_id}/{topic_id}"
            )
        return session

    def _open_of(self, user_id: str) -> set[str]:
        """The ids of the unended sessions that are `user_id`'s, active or left open."""
        return {value for (owner, _, _), value in self._open.items() if owner == user_id}

    def _one_open(self, user_id: str) -> tuple[tuple[str, str, str], str]:
        """The one unended session that blocks a start by `user_id`, and where it is.

        Any unended session blocks it, because a backend has room for one active session whoever it
        belongs to. When the scan found more than one -- two PCs each left theirs behind, say -- the
        one the asking user owns is the one they are named, because it is the one they can still
        resume or end; another user's is `OtherUserSessionOpenError` and names none.
        """
        mine = next((item for item in self._open.items() if item[0][0] == user_id), None)
        return mine if mine is not None else next(iter(self._open.items()))

    async def _listed(self, user_id: str | None, session_id: str) -> bool:
        """Whether a topic lists `session_id`, ended or not: `user_id`'s, or any user's if none.

        The search over every user is what `end` needs, since it is not told whose the session is;
        it settles ended-against-unknown and never hands back another student's session.
        """
        vaults = [self._users[user_id]] if user_id is not None else list(self._users.values())
        return await asyncio.to_thread(
            lambda: any(_topic_lists(vault, session_id) for vault in vaults)
        )

    async def _user_scope(self, user_id: str | None) -> tuple[str, Vault]:
        """The user a call acts for, and the handle on their folder of the vault.

        `user_id` names them; `None` is protocol 1.8's single-user fallback -- the vault's only
        user, which is what keeps a caller that knows nothing about users working on a vault
        nobody shares. Handles are cached, because `for_user` reads the file system to check the
        user exists and a lifecycle call should do that once per user, not once per request.

        Raises:
            VaultUnavailableError: the vault cannot be opened.
            NoUserError: no user was named and the vault does not hold exactly one.
            UnknownUserError: the vault has no such user.
        """
        root = await self._ready()
        wanted = user_id
        if wanted is None:
            ids = await asyncio.to_thread(user_ids, root)
            if len(ids) != 1:
                raise NoUserError(
                    f"no user was named and the vault at {root.root} holds {len(ids)}: a call has"
                    " to say which student it acts for"
                )
            wanted = ids[0]
        handle = self._users.get(wanted)
        if handle is None:
            try:
                handle = await asyncio.to_thread(root.for_user, wanted)
            except UserNotFoundError as error:
                raise UnknownUserError(wanted) from error
            self._users[wanted] = handle
        return wanted, handle

    async def _ready(self) -> Vault:
        """The vault's root handle, opened, pulled and scanned for every user's unended sessions."""
        if self._loaded and self._root is not None:
            return self._root
        async with self._lock:
            if not self._loaded:
                given = self._vault
                root = await self._call(Vault.open, self._settings.path) if given is None else given
                root = _root_handle(root)
                if self._sync is None:
                    self._sync = GitSync(root, self._settings.git)
                # Pull before the scan, so sessions another PC left open are seen. A conflict is
                # only logged here: the next session start pulls again and refuses on it.
                await self._pull("vault open")
                await self._check_host(root)
                self._users, self._open = await asyncio.to_thread(_scan_open_sessions, root)
                self._index = await self._open_index(root)
                self._vault = root
                self._root = root
                self._loaded = True
                self._start_runner()
                for hook in self._on_open:
                    try:
                        hook(root)
                    except Exception:
                        logger.exception("vault open hook %r failed", hook)
        assert self._root is not None
        return self._root

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
        user_id: str,
        subject_id: str,
        topic_id: str,
        title: str,
        last_session_at_ms: int | None = None,
        pending_count: int | None = None,
        digest_excerpt: str | None = None,
    ) -> protocol.Topic:
        return protocol.Topic(
            topic_id=topic_id,
            subject_id=subject_id,
            name=title,
            open_session_id=self._open.get((user_id, subject_id, topic_id)),
            last_session_at_ms=last_session_at_ms,
            pending_count=pending_count,
            digest_excerpt=digest_excerpt,
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
    """The topic's latest study session start (epoch ms) and open pending count, `None` if unknown.

    A review session (a doubt's resolution) is not a study session, so it is left out (#191).

    Each is read on its own through the vault's and the observer's public functions; one that
    cannot be read is logged and left out, so a damaged session never hides the topic list.
    """
    last: int | None = None
    try:
        sessions = list_sessions(vault, subject_id, topic_id)
    except VaultError as error:
        logger.warning("topic %s/%s: sessions unreadable: %s", subject_id, topic_id, error)
    else:
        studied = [meta.started_at for meta in sessions if meta.is_study]
        if studied:
            last = _epoch_ms(max(studied))
    pending: int | None = None
    try:
        snapshot = load_observer_snapshot(vault, subject_id, topic_id, write_back=False)
    except (VaultError, ObserverStateError) as error:
        logger.warning("topic %s/%s: observer state unreadable: %s", subject_id, topic_id, error)
    else:
        pending = len(snapshot.state.open_pending())
    return last, pending


def _topic_excerpt(vault: Vault, subject_id: str, topic_id: str) -> str | None:
    """The summary paragraph of the topic's digest, `None` before its first session end.

    A digest that cannot be read is logged and left out, like `_topic_activity`.
    """
    try:
        text = topic_digest(vault, subject_id, topic_id)
    except VaultError as error:
        logger.warning("topic %s/%s: digest unreadable: %s", subject_id, topic_id, error)
        return None
    return digest_excerpt(text, protocol.DIGEST_EXCERPT_MAX)


def _scan_open_sessions(
    root: Vault,
) -> tuple[dict[str, Vault], dict[tuple[str, str, str], str]]:
    """Every user's handle, and the unended session of every one of their topics.

    The scan is what makes a session a student left unended -- here or on another PC, whose pull
    has just brought it in -- block a new start by anybody, and what lets a topic list its own
    `open_session_id` after a restart. A user whose folder holds no readable `profile.json` gets no
    handle and is skipped with a warning: one damaged student must not stop the others capturing.
    """
    users: dict[str, Vault] = {}
    found: dict[tuple[str, str, str], str] = {}
    for user_id in user_ids(root):
        try:
            vault = root.for_user(user_id)
        except UserNotFoundError as error:
            logger.warning("vault user %s has no handle; not scanned: %s", user_id, error)
            continue
        users[user_id] = vault
        for subject in list_subjects(vault):
            for topic in list_topics(vault, subject.slug):
                if not topic.topic.sessions:
                    continue
                try:
                    session = resume_session(vault, subject.slug, topic.slug)
                except NoOpenSessionError:
                    continue
                found[(user_id, subject.slug, topic.slug)] = session.id
    return users, found


def _topic_lists(vault: Vault, session_id: str) -> bool:
    """Whether some topic of this handle lists `session_id`, ended or not. Blocking file I/O."""
    for subject in list_subjects(vault):
        for topic in list_topics(vault, subject.slug):
            if session_id in topic.topic.sessions:
                return True
    return False


def _key(session: Session) -> tuple[str, str, str]:
    """The `_open` key of a session: whose it is, and the topic it belongs to."""
    return (_user_of(session), session.subject_slug, session.topic_slug)


def _user_of(session: Session) -> str:
    """The user a session handle belongs to: every one this service builds is a user's."""
    user_id = session.vault.user_id
    assert user_id is not None, "a session handle is built from a user's vault handle"
    return user_id


def _root_handle(vault: Vault) -> Vault:
    """The root handle of `vault`: `for_user` needs one, and a caller may have given either."""
    return vault if vault.user_id is None else replace(vault, path=vault.root, user_id=None)


def _subject(stored: StoredSubject) -> protocol.Subject:
    return protocol.Subject(subject_id=stored.slug, name=stored.subject.name)


def _open_session(session: Session) -> OpenSession:
    return OpenSession(
        session_id=session.id,
        user_id=_user_of(session),
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
    "OTHER_USER_SESSION_OPEN_DETAIL",
    "SESSION_ENDED",
    "SESSION_RESUMED",
    "SESSION_STARTED",
    "ActiveSessionExistsError",
    "EndHook",
    "EndReason",
    "LifecycleError",
    "NoUserError",
    "OpenSession",
    "OtherUserSessionOpenError",
    "SessionAlreadyEndedError",
    "SessionConflictError",
    "SessionService",
    "UnknownSessionError",
    "UnknownUserError",
    "VaultSyncConflictError",
    "VaultUnavailableError",
    "stored_captures",
]
