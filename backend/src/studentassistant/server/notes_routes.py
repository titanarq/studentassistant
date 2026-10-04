"""`POST /api/subjects/{subject_id}/topics/{topic_id}/notes/generate`: "prepárame el tema".

Thin: the work is `editor.generate.generate_notes`. The route opens the vault through the
`SessionService` (so the pulled vault and its `GitSync` are the ones every other route uses),
builds an `editor` client bound to the topic's cost ledger and waits for the generation, which
answers with the `GenerationResult`. One generation per topic runs at a time. The
`notes.generated` event is published on the bus (origin `editor`, persisted) when the topic's
session is the active one; it is always recorded in the topic's `conversations/editor.jsonl`.

Available only when the app has an `llm_transport` (`serve` passes the real one): without it,
nothing ever calls Claude and the route answers 503. Errors, as `{"detail": "...", "code"?: "..."}`
in Spanish (`server.errors`): an unknown topic 404, a generation of that topic already running
409, a reached cost cap 409 `cost_cap_reached` until the request says `confirm_over_cap`, a Claude
failure or refusal 502, a vault that cannot be opened 503.

Ending a session never starts a generation (#440): the session-end `prepare_notes` flag of
protocol 1.6 is accepted and ignored, and its polling route `GET .../notes/generation` is gone.

A generation that wrote the notes (not a draft) is a `notes.changed` (origin `generation`) on the
topic's workspace stream (`workspace.py`), whichever way it was started (this route or a
"prepárame el tema" chat request through `assistant_requests.py`).

**Batched mode** (#326, `[editor] prepare_mode = "batched"`, the default): a generation is
`editor.incorporate.incorporate_pending` -- the topic's pending sources incorporated in
sequential small batches of `[editor] incorporate_batch_size`, each batch a chat turn of kind
`incorporate` on the workspace stream (`turn.started`, its reply, `turn.result`, `notes.changed`
origin `editor`) followed by `incorporation.progress` `{done, total, source_ids}`; at the end,
when something changed, the next notes version is tagged. Its result is answered as a
`GenerationResult` (never a draft; `version`/`tag` only when the notes changed). `single` keeps
the one-call `generate_notes`. `NotesGenerator.incorporate` is one incorporation of a few sources
("incorpora la página 3") for the chat router (#327), under the caller's claim of the notes lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel

from studentassistant.config import Settings
from studentassistant.editor.generate import GenerationResult, generate_notes
from studentassistant.editor.incorporate import (
    IncorporationError,
    IncorporationResult,
    PendingIncorporationResult,
    incorporate_pending,
    incorporate_sources,
)
from studentassistant.editor.revise import ChatRequestRef, ReplySink
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    Transport,
    get_client,
)
from studentassistant.observer import topic_digest
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import cost_cap_error
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.server.workspace import (
    INCORPORATION_PROGRESS,
    TurnBroadcast,
    WorkspaceHub,
)

if TYPE_CHECKING:
    from studentassistant.server.doubt_chat import DoubtChat
from studentassistant.vault import (
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
    notes_path,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

UNAVAILABLE_DETAIL = "La generación de apuntes no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "Ya se están generando los apuntes de este tema."
REFUSED_DETAIL = "Claude se ha negado a escribir los apuntes de este tema."
FAILED_DETAIL = "No se han podido generar los apuntes: Claude no ha respondido. Prueba más tarde."
ERROR_DETAIL = "No se han podido generar los apuntes por un error del servidor."
CONFIRM_SENTENCE = "Confirma para generar los apuntes igualmente."


class GenerateNotesRequest(BaseModel):
    """The optional body: `confirm_over_cap` proceeds past a reached cost cap."""

    confirm_over_cap: bool = False


TURN_HOLDER = "turn"
"""The notes-lock holder of an editor chat turn or "¿por qué?": it applies its change under the
short write lock and re-reads the notes, so a student save does not have to wait for it."""


class NotesGenerator:
    """What the notes routes need to call the editor: settings, transport, one lock per topic."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        *,
        workspace: WorkspaceHub | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.workspace = workspace
        """Where a generation that wrote the notes publishes `notes.changed` (None: nowhere)."""
        self.doubts: DoubtChat | None = None
        """The app's doubts asker (#325): a generation's doubts go to the live session through
        it, and it asks the next doubt after a generation that wrote the notes."""
        self._running: dict[tuple[str | None, str, str], str] = {}

    def _key(
        self, subject_id: str, topic_id: str, user_id: str | None
    ) -> tuple[str | None, str, str]:
        """The lock's key: the student's too, so two students' same-named topics are two locks.

        A caller that names no user is the vault's only one, as in the workspace hub.
        """
        if self.workspace is not None:
            user_id = self.workspace.resolve_user(user_id)
        return (user_id, subject_id, topic_id)

    def claim(
        self,
        subject_id: str,
        topic_id: str,
        holder: str = "editor",
        *,
        user_id: str | None = None,
    ) -> bool:
        """Take the topic's notes lock for `holder` (`TURN_HOLDER` for an editor chat turn, which
        a student save may interleave with); `False` when something already holds it."""
        key = self._key(subject_id, topic_id, user_id)
        if key in self._running:
            return False
        self._running[key] = holder
        return True

    def release(self, subject_id: str, topic_id: str, *, user_id: str | None = None) -> None:
        self._running.pop(self._key(subject_id, topic_id, user_id), None)

    def holder(self, subject_id: str, topic_id: str, *, user_id: str | None = None) -> str | None:
        """Who holds the topic's notes lock now, `None` when nothing does."""
        return self._running.get(self._key(subject_id, topic_id, user_id))

    async def generate(
        self,
        sessions: SessionService,
        subject_id: str,
        topic_id: str,
        *,
        confirm_over_cap: bool = False,
        user_id: str | None = None,
    ) -> GenerationResult:
        """Run one generation of a topic the caller has `claim`ed; errors are re-raised.

        `user_id` is the student whose topic it is (`SessionService.consumer_scope`)."""
        result = await self._generate(sessions, subject_id, topic_id, confirm_over_cap, user_id)
        if self.workspace is not None and not result.draft and result.version is not None:
            self.workspace.notes_changed(
                subject_id,
                topic_id,
                revision=result.revision,
                origin="generation",
                summary=f"Apuntes preparados (versión {result.version})."
                if result.version is not None
                else "Apuntes preparados.",
                user_id=user_id,
            )
        if self.doubts is not None and not result.draft:
            self.doubts.schedule(subject_id, topic_id, user_id=user_id)
        return result

    async def incorporate(
        self,
        sessions: SessionService,
        subject_id: str,
        topic_id: str,
        source_ids: list[str],
        *,
        on_reply: ReplySink | None = None,
        request: ChatRequestRef | None = None,
        turn_id: str | None = None,
        confirm_over_cap: bool = False,
        user_id: str | None = None,
    ) -> IncorporationResult:
        """One incorporation of a few sources ("incorpora la página 3", #326) of a topic the
        caller has `claim`ed (as `TURN_HOLDER`, like an editor chat turn); the chat router
        (#327) calls it. The reply streams to `on_reply`; `notes.incorporated` goes on the bus
        and the doubts to the live session as a revision turn's. Publishing the turn on the
        workspace stream is the caller's (a `TurnBroadcast` of kind `incorporate`).

        Raises what `editor.incorporate.incorporate_sources` raises (an `IncorporationError`
        with a Spanish message for a refused request), and `VaultUnavailableError`.
        """
        vault, sync, client, publish = await self._editor(sessions, subject_id, topic_id, user_id)
        return await incorporate_sources(
            vault,
            subject_id,
            topic_id,
            source_ids,
            client=client,
            sync=sync,
            on_reply=on_reply,
            on_event=publish,
            request=request,
            confirm_over_cap=confirm_over_cap,
            turn_id=turn_id,
            max_sources=self.settings.editor.incorporate_max_sources,
            host=sessions.host,
            live=None
            if self.doubts is None
            else self.doubts.live(subject_id, topic_id, user_id=user_id),
        )

    async def _editor(
        self, sessions: SessionService, subject_id: str, topic_id: str, user_id: str | None = None
    ) -> tuple[Vault, Any, Any, Callable[[str, dict[str, Any]], Any]]:
        """The vault, its sync, an `editor` client bound to the topic's ledger and the bus
        publisher of the topic's active session (the student's own, when `user_id` is named)."""
        vault, sync = await sessions.consumer_scope(user_id)

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if (
                active is not None
                and (active.subject_id, active.topic_id) == (subject_id, topic_id)
                and (user_id is None or active.user_id == user_id)
            ):
                await sessions.bus.publish(active.session_id, kind, "editor", payload)

        client = get_client(
            "editor",
            settings=self.settings,
            transport=self.transport,
            ledger=LedgerBinding(vault, subject_id, topic_id),
        )
        return vault, sync, client, publish

    async def _generate(
        self,
        sessions: SessionService,
        subject_id: str,
        topic_id: str,
        confirm_over_cap: bool,
        user_id: str | None,
    ) -> GenerationResult:
        if self.settings.editor.prepare_mode == "batched":
            return await self._generate_batched(
                sessions, subject_id, topic_id, confirm_over_cap, user_id
            )
        vault, sync, client, publish = await self._editor(sessions, subject_id, topic_id, user_id)
        return await generate_notes(
            vault,
            subject_id,
            topic_id,
            client=client,
            sync=sync,
            digest=topic_digest,
            on_event=publish,
            confirm_over_cap=confirm_over_cap,
            host=sessions.host,
            live=None
            if self.doubts is None
            else self.doubts.live(subject_id, topic_id, user_id=user_id),
        )

    async def _generate_batched(
        self,
        sessions: SessionService,
        subject_id: str,
        topic_id: str,
        confirm_over_cap: bool,
        user_id: str | None,
    ) -> GenerationResult:
        vault, sync, client, publish = await self._editor(sessions, subject_id, topic_id, user_id)
        hub = self.workspace

        def progress(payload: dict[str, Any]) -> None:
            if hub is not None:
                hub.publish(subject_id, topic_id, INCORPORATION_PROGRESS, payload, user_id=user_id)

        settings = self.settings.editor
        result = await incorporate_pending(
            vault,
            subject_id,
            topic_id,
            client=client,
            sync=sync,
            batch_size=settings.incorporate_batch_size,
            max_sources=settings.incorporate_max_sources,
            on_event=publish,
            on_progress=progress,
            turns=None if hub is None else _BatchTurns(hub, subject_id, topic_id, user_id),
            confirm_over_cap=confirm_over_cap,
            host=sessions.host,
            live=None
            if self.doubts is None
            else self.doubts.live(subject_id, topic_id, user_id=user_id),
        )
        return _as_generation(vault, result)


class _BatchTurns:
    """Each batch of a batched "prepárame el tema" as a chat turn of kind `incorporate`."""

    def __init__(
        self, hub: WorkspaceHub, subject_id: str, topic_id: str, user_id: str | None = None
    ) -> None:
        self.hub, self.subject_id, self.topic_id = hub, subject_id, topic_id
        self.user_id = user_id
        self._current: TurnBroadcast | None = None

    def begin(self, source_ids: list[str]) -> tuple[str | None, ReplySink | None]:
        broadcast = TurnBroadcast(
            self.hub,
            self.subject_id,
            self.topic_id,
            origin="typed",
            kind="incorporate",
            user_id=self.user_id,
        )
        broadcast.started()
        self._current = broadcast
        return broadcast.turn_id, broadcast.reply

    def end(self, result: IncorporationResult | None, error: BaseException | None) -> None:
        broadcast, self._current = self._current, None
        if broadcast is None:
            return
        if result is not None:
            broadcast.result(result)
        elif isinstance(error, IncorporationError):
            broadcast.error(422, str(error))
        elif isinstance(error, CostConfirmationRequiredError):
            refused = cost_cap_error(error, CONFIRM_SENTENCE)
            code = refused.code
            broadcast.error(
                refused.status_code, refused.detail, None if code is None else code.value
            )
        elif isinstance(error, RefusalError):
            broadcast.error(502, REFUSED_DETAIL)
        else:
            broadcast.error(502, FAILED_DETAIL)


def _as_generation(vault: Vault, result: PendingIncorporationResult) -> GenerationResult:
    """A batched run answered as the `GenerationResult` the routes and the stream already use."""
    path = notes_path(vault, result.subject, result.topic).relative_to(vault.path).as_posix()
    return GenerationResult(
        subject=result.subject,
        topic=result.topic,
        draft=False,
        path=path,
        version=result.version,
        tag=result.tag,
        commit=result.commit,
        attempts=result.attempts,
        warning=result.warning,
        contradictions=result.contradictions,
        doubts=result.doubts,
        model=result.model or "",
        revision=result.revision,
    )


def notes_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/notes/generate")
    async def generate(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        body: GenerateNotesRequest | None = None,
    ) -> GenerationResult:
        generator: NotesGenerator | None = request.app.state.notes
        if generator is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        sessions: SessionService = request.app.state.sessions
        vault = await _open_vault(sessions)
        await _require_topic(vault, subject_id, topic_id)
        if not generator.claim(subject_id, topic_id):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        try:
            return await generator.generate(
                sessions,
                subject_id,
                topic_id,
                confirm_over_cap=bool(body and body.confirm_over_cap),
            )
        except CostConfirmationRequiredError as error:
            raise cost_cap_error(error, CONFIRM_SENTENCE) from error
        except IncorporationError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RefusalError as error:
            raise HTTPException(status_code=502, detail=REFUSED_DETAIL) from error
        except LLMError as error:
            logger.warning("notes generation of %s/%s failed: %s", subject_id, topic_id, error)
            raise HTTPException(status_code=502, detail=FAILED_DETAIL) from error
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        finally:
            generator.release(subject_id, topic_id)

    return router


async def _open_vault(sessions: SessionService) -> Vault:
    try:
        return await sessions.open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error


async def _require_topic(vault: Vault, subject_id: str, topic_id: str) -> None:
    try:
        await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
