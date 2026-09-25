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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel

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
from studentassistant.vault import SubjectNotFoundError, TopicNotFoundError, get_topic

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

UNAVAILABLE_DETAIL = "La generación de apuntes no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "Ya se están generando los apuntes de este tema."
REFUSED_DETAIL = "Claude se ha negado a escribir los apuntes de este tema."
FAILED_DETAIL = "No se han podido generar los apuntes: Claude no ha respondido. Prueba más tarde."
CONFIRM_SENTENCE = "Confirma para generar los apuntes igualmente."


class GenerateNotesRequest(BaseModel):
    """The optional body: `confirm_over_cap` proceeds past a reached cost cap."""

    confirm_over_cap: bool = False


class NotesGenerator:
    """What the route needs to call the editor: settings, transport and one lock per topic."""

    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings = settings
        self.transport = transport
        self._running: set[tuple[str, str]] = set()

    def claim(self, subject_id: str, topic_id: str) -> bool:
        key = (subject_id, topic_id)
        if key in self._running:
            return False
        self._running.add(key)
        return True

    def release(self, subject_id: str, topic_id: str) -> None:
        self._running.discard((subject_id, topic_id))


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
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        sync = sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL)
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        if not generator.claim(subject_id, topic_id):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await sessions.bus.publish(active.session_id, kind, "editor", payload)

        try:
            client = get_client(
                "editor",
                settings=generator.settings,
                transport=generator.transport,
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
                confirm_over_cap=bool(body and body.confirm_over_cap),
            )
        except CostConfirmationRequiredError as error:
            raise cost_cap_error(error, CONFIRM_SENTENCE) from error
        except RefusalError as error:
            raise HTTPException(status_code=502, detail=REFUSED_DETAIL) from error
        except LLMError as error:
            logger.warning("notes generation of %s/%s failed: %s", subject_id, topic_id, error)
            raise HTTPException(status_code=502, detail=FAILED_DETAIL) from error
        finally:
            generator.release(subject_id, topic_id)

    return router
