"""`POST /api/subjects/{subject_id}/topics/{topic_id}/notes/generate`: "prepárame el tema".

Also `GET .../notes/generation`, the status of the topic's latest generation.

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

Ending a session with `prepare_notes: true` (protocol 1.6, `session_routes.py`) starts the same
generation in a background task (`NotesGenerator.start_background`), under the same per-topic
lock. `GET .../notes/generation` answers the topic's latest generation, background or not, as
`protocol.NotesGenerationStatus` (`idle` | `running` | `done` | `failed` | `needs_confirmation`),
kept in memory since the backend started; a reached cost cap is `needs_confirmation` and the
student confirms through `POST .../notes/generate` with `confirm_over_cap`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel

from studentassistant import protocol
from studentassistant.config import Settings
from studentassistant.editor.generate import GenerationResult, generate_notes
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
from studentassistant.vault import SubjectNotFoundError, TopicNotFoundError, Vault, get_topic

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


UNAVAILABLE_DETAIL = "La generación de apuntes no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "Ya se están generando los apuntes de este tema."
REFUSED_DETAIL = "Claude se ha negado a escribir los apuntes de este tema."
FAILED_DETAIL = "No se han podido generar los apuntes: Claude no ha respondido. Prueba más tarde."
ERROR_DETAIL = "No se han podido generar los apuntes por un error del servidor."
SHUTDOWN_TIMEOUT_SECONDS = 5.0
CONFIRM_SENTENCE = "Confirma para generar los apuntes igualmente."


class GenerateNotesRequest(BaseModel):
    """The optional body: `confirm_over_cap` proceeds past a reached cost cap."""

    confirm_over_cap: bool = False


TURN_HOLDER = "turn"
"""The notes-lock holder of an editor chat turn or "¿por qué?": it applies its change under the
short write lock and re-reads the notes, so a student save does not have to wait for it."""


class NotesGenerator:
    """What the notes routes need to call the editor: settings, transport, one lock per topic.

    It also keeps each topic's latest generation (`status`), in memory since the backend
    started, and runs the background ones a session end asks for (`start_background`).
    """

    def __init__(
        self, settings: Settings, transport: Transport, *, clock: Clock = _utc_now
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._clock = clock
        self._running: dict[tuple[str, str], str] = {}
        self._statuses: dict[tuple[str, str], protocol.NotesGenerationStatus] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def claim(self, subject_id: str, topic_id: str, holder: str = "editor") -> bool:
        """Take the topic's notes lock for `holder` (`TURN_HOLDER` for an editor chat turn, which
        a student save may interleave with); `False` when something already holds it."""
        key = (subject_id, topic_id)
        if key in self._running:
            return False
        self._running[key] = holder
        return True

    def release(self, subject_id: str, topic_id: str) -> None:
        self._running.pop((subject_id, topic_id), None)

    def holder(self, subject_id: str, topic_id: str) -> str | None:
        """Who holds the topic's notes lock now, `None` when nothing does."""
        return self._running.get((subject_id, topic_id))

    def status(self, subject_id: str, topic_id: str) -> protocol.NotesGenerationStatus:
        """The topic's latest generation; `idle` when none ran since the backend started."""
        stored = self._statuses.get((subject_id, topic_id))
        if stored is not None:
            return stored
        return protocol.NotesGenerationStatus(
            subject_id=subject_id, topic_id=topic_id, status="idle"
        )

    async def generate(
        self,
        sessions: SessionService,
        subject_id: str,
        topic_id: str,
        *,
        confirm_over_cap: bool = False,
    ) -> GenerationResult:
        """Run one generation of a topic the caller has `claim`ed, recording its status.

        The status is `running` meanwhile, then `done`, `needs_confirmation` (the
        `CostConfirmationRequiredError` is re-raised) or `failed` (the error is re-raised).
        """
        key = (subject_id, topic_id)
        started = self._now_ms()
        self._statuses[key] = protocol.NotesGenerationStatus(
            subject_id=subject_id, topic_id=topic_id, status="running", started_at_ms=started
        )

        def finish(status: protocol.NotesGenerationState, **fields: Any) -> None:
            self._statuses[key] = protocol.NotesGenerationStatus(
                subject_id=subject_id,
                topic_id=topic_id,
                status=status,
                started_at_ms=started,
                finished_at_ms=self._now_ms(),
                **fields,
            )

        try:
            result = await self._generate(sessions, subject_id, topic_id, confirm_over_cap)
        except CostConfirmationRequiredError as error:
            finish("needs_confirmation", detail=cost_cap_error(error, CONFIRM_SENTENCE).detail)
            raise
        except RefusalError:
            finish("failed", detail=REFUSED_DETAIL)
            raise
        except LLMError:
            finish("failed", detail=FAILED_DETAIL)
            raise
        except Exception:
            finish("failed", detail=ERROR_DETAIL)
            raise
        finish("done", version=result.version, draft=result.draft, warning=result.warning)
        return result

    def start_background(
        self, sessions: SessionService, subject_id: str, topic_id: str
    ) -> protocol.NotesGenerationStart:
        """Start generating the topic's notes in a background task, unless one is running.

        Answers `started`, or `running` when a generation of the topic (background or
        `POST .../notes/generate`) already holds its lock; nothing is started then.
        """
        if not self.claim(subject_id, topic_id):
            return "running"
        # `running` from now on, so a client polling right after the end never reads `idle`.
        self._statuses[(subject_id, topic_id)] = protocol.NotesGenerationStatus(
            subject_id=subject_id,
            topic_id=topic_id,
            status="running",
            started_at_ms=self._now_ms(),
        )
        task = asyncio.create_task(
            self._background(sessions, subject_id, topic_id),
            name=f"notes-generation-{subject_id}-{topic_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return "started"

    async def wait_background(self) -> None:
        """Wait for the background generations running now (tests; the app's shutdown)."""
        while self._tasks:
            await asyncio.wait(set(self._tasks))

    async def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Give the background generations `timeout` seconds to finish, then cancel them."""
        tasks = set(self._tasks)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    async def _background(self, sessions: SessionService, subject_id: str, topic_id: str) -> None:
        try:
            await self.generate(sessions, subject_id, topic_id)
        except CostConfirmationRequiredError:
            logger.info(
                "background notes generation of %s/%s awaits confirmation", subject_id, topic_id
            )
        except Exception:
            logger.exception("background notes generation of %s/%s failed", subject_id, topic_id)
        finally:
            self.release(subject_id, topic_id)

    async def _generate(
        self, sessions: SessionService, subject_id: str, topic_id: str, confirm_over_cap: bool
    ) -> GenerationResult:
        vault = await sessions.open_vault()
        sync = sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise VaultUnavailableError("the vault has no sync")

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await sessions.bus.publish(active.session_id, kind, "editor", payload)

        client = get_client(
            "editor",
            settings=self.settings,
            transport=self.transport,
            ledger=LedgerBinding(vault, subject_id, topic_id),
        )
        return await generate_notes(
            vault,
            subject_id,
            topic_id,
            client=client,
            sync=sync,
            digest=topic_digest,
            on_event=publish,
            confirm_over_cap=confirm_over_cap,
        )

    def _now_ms(self) -> int:
        return int(self._clock().timestamp() * 1000)


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
        except RefusalError as error:
            raise HTTPException(status_code=502, detail=REFUSED_DETAIL) from error
        except LLMError as error:
            logger.warning("notes generation of %s/%s failed: %s", subject_id, topic_id, error)
            raise HTTPException(status_code=502, detail=FAILED_DETAIL) from error
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        finally:
            generator.release(subject_id, topic_id)

    @router.get(
        "/api/subjects/{subject_id}/topics/{topic_id}/notes/generation",
        response_model_exclude_none=True,
    )
    async def generation_status(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> protocol.NotesGenerationStatus:
        sessions: SessionService = request.app.state.sessions
        vault = await _open_vault(sessions)
        await _require_topic(vault, subject_id, topic_id)
        generator: NotesGenerator | None = request.app.state.notes
        if generator is None:
            return protocol.NotesGenerationStatus(
                subject_id=subject_id, topic_id=topic_id, status="idle"
            )
        return generator.status(subject_id, topic_id)

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
