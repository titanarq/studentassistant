"""The doubts API for the web's pending panel: list, review, answer and dismiss (`editor.doubts`).

Thin: the work is `studentassistant.editor.doubts`. Every route opens the vault through the
`SessionService` (the pulled vault and its `GitSync` every other route uses) and runs one doubts
operation of a topic at a time. Reviewing and answering call the `editor` role through the app's
`llm_transport`, bound to the topic's cost ledger, and hold the topic's notes lock with "prepárame
el tema" (`NotesGenerator.claim`), since both write the notes; without a transport they are 503.
Listing and dismissing never call Claude. Reviewing and answering hold the lock as a
`TURN_HOLDER`, since they apply their edits under the short write lock on the latest notes (#325):
a student save interleaves with them and the editor is re-asked on the new notes.

With an unended session of the topic (#325) the doubts' events go to that live session through
the bus (`DoubtChat.live`), so the routes work during a session too; an unended session this
backend does not have active is still `session_open`. An answer or a dismissal is announced on
the topic's workspace stream (`doubt.resolved`, and `notes.changed` origin `editor` when the notes
changed), a review's auto-resolutions as `doubts.auto_resolved`; then the chat asks the next doubt
(`DoubtChat.schedule`).

Errors, as `{"detail": "...", "code"?: "..."}` in Spanish (`server.errors`): an unknown topic or
doubt 404; a doubt already closed (`doubt_closed`), a topic with an unended session
(`session_open`), a review without notes, another operation of the topic running, or a reached
cost cap (`cost_cap_reached`, until the request says `confirm_over_cap`) 409; an answer that
does not fit the question 422; a Claude failure or refusal 502; a vault that cannot be opened 503.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel

from studentassistant.editor.doubts import (
    DoubtAnswer,
    DoubtClosedError,
    DoubtError,
    DoubtsQueue,
    InvalidAnswerError,
    OpenSessionError,
    ResolutionResult,
    ReviewResult,
    UnknownDoubtError,
    answer_doubt,
    dismiss_doubt,
    list_doubts,
    review_doubts,
)
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMClient,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.doubt_chat import DoubtChat
from studentassistant.server.errors import ApiError, cost_cap_error
from studentassistant.server.notes_routes import TURN_HOLDER, NotesGenerator
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    GitSync,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]
PendingId = Annotated[str, Path(min_length=1, max_length=200)]

UNAVAILABLE_DETAIL = (
    "La resolución de dudas con el editor no está disponible: el servidor no usa Claude."
)
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "El editor ya está trabajando en los apuntes o las dudas de este tema."
REFUSED_DETAIL = "Claude se ha negado a resolver las dudas de este tema."
FAILED_DETAIL = "No se han podido resolver las dudas: Claude no ha respondido. Prueba más tarde."
CONFIRM_SENTENCE = "Confirma para continuar igualmente."


class ReviewRequest(BaseModel):
    """The optional body of a review: `confirm_over_cap` proceeds past a reached cost cap."""

    confirm_over_cap: bool = False


class AnswerRequest(DoubtAnswer):
    """The student's answer (see `DoubtAnswer`) plus `confirm_over_cap`."""

    confirm_over_cap: bool = False


def _status(error: DoubtError) -> int:
    if isinstance(error, UnknownDoubtError):
        return 404
    if isinstance(error, InvalidAnswerError):
        return 422
    return 409  # closed already, an unended session, no notes yet


def _code(error: DoubtError) -> ErrorCode | None:
    if isinstance(error, DoubtClosedError):
        return ErrorCode.DOUBT_CLOSED
    if isinstance(error, OpenSessionError):
        return ErrorCode.SESSION_OPEN
    return None


def doubts_router() -> APIRouter:
    router = APIRouter()
    running: set[tuple[str, str]] = set()

    async def open_topic(request: Request, subject_id: str, topic_id: str) -> tuple[Vault, GitSync]:
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
        return vault, sync

    @asynccontextmanager
    async def exclusive(
        request: Request, subject_id: str, topic_id: str, *, editor: bool
    ) -> AsyncIterator[NotesGenerator | None]:
        """One doubts operation of the topic at a time; `editor` ones also take the notes lock."""
        generator: NotesGenerator | None = request.app.state.notes
        if editor and generator is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        key = (subject_id, topic_id)
        if key in running:
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        if (
            editor
            and generator is not None
            and not generator.claim(subject_id, topic_id, TURN_HOLDER)
        ):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        running.add(key)
        try:
            yield generator
        except DoubtError as error:
            code = _code(error)
            if code is not None:
                raise ApiError(_status(error), str(error), code) from error
            raise HTTPException(status_code=_status(error), detail=str(error)) from error
        except CostConfirmationRequiredError as error:
            raise cost_cap_error(error, CONFIRM_SENTENCE) from error
        except RefusalError as error:
            raise HTTPException(status_code=502, detail=REFUSED_DETAIL) from error
        except LLMError as error:
            logger.warning("doubts of %s/%s failed: %s", subject_id, topic_id, error)
            raise HTTPException(status_code=502, detail=FAILED_DETAIL) from error
        finally:
            running.discard(key)
            if editor and generator is not None:
                generator.release(subject_id, topic_id)

    def doubt_chat(request: Request) -> DoubtChat:
        chat: DoubtChat | None = getattr(request.app.state, "doubt_chat", None)
        if chat is None:  # pragma: no cover - the app always has one
            chat = DoubtChat(request.app.state.sessions, request.app.state.workspace)
            request.app.state.doubt_chat = chat
        return chat

    def editor_client(generator: NotesGenerator, vault: Vault, s: str, t: str) -> LLMClient:
        return get_client(
            "editor",
            settings=generator.settings,
            transport=generator.transport,
            ledger=LedgerBinding(vault, s, t),
        )

    @router.get("/api/subjects/{subject_id}/topics/{topic_id}/doubts")
    async def doubts(request: Request, subject_id: SubjectId, topic_id: TopicId) -> DoubtsQueue:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(list_doubts, vault, subject_id, topic_id)

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/doubts/review")
    async def review(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        body: ReviewRequest | None = None,
    ) -> ReviewResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        sessions: SessionService = request.app.state.sessions
        chat = doubt_chat(request)
        async with exclusive(request, subject_id, topic_id, editor=True) as generator:
            assert generator is not None
            result = await review_doubts(
                vault,
                subject_id,
                topic_id,
                client=editor_client(generator, vault, subject_id, topic_id),
                sync=sync,
                host=sessions.host,
                confirm_over_cap=bool(body and body.confirm_over_cap),
                live=chat.live(subject_id, topic_id),
            )
        chat.reviewed(subject_id, topic_id, result)
        chat.schedule(subject_id, topic_id)
        return result

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/doubts/{pending_id}/answer")
    async def answer(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        pending_id: PendingId,
        body: AnswerRequest,
    ) -> ResolutionResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        sessions: SessionService = request.app.state.sessions
        chat = doubt_chat(request)
        async with exclusive(request, subject_id, topic_id, editor=True) as generator:
            assert generator is not None
            result = await answer_doubt(
                vault,
                subject_id,
                topic_id,
                pending_id,
                DoubtAnswer.model_validate(body.model_dump(exclude={"confirm_over_cap"})),
                client=editor_client(generator, vault, subject_id, topic_id),
                sync=sync,
                host=sessions.host,
                confirm_over_cap=body.confirm_over_cap,
                live=chat.live(subject_id, topic_id),
            )
        chat.resolved(subject_id, topic_id, result)
        chat.schedule(subject_id, topic_id)
        return result

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/doubts/{pending_id}/dismiss")
    async def dismiss(
        request: Request, subject_id: SubjectId, topic_id: TopicId, pending_id: PendingId
    ) -> ResolutionResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        sessions: SessionService = request.app.state.sessions
        chat = doubt_chat(request)
        async with exclusive(request, subject_id, topic_id, editor=False):
            result = await dismiss_doubt(
                vault,
                subject_id,
                topic_id,
                pending_id,
                sync=sync,
                host=sessions.host,
                live=chat.live(subject_id, topic_id),
            )
        chat.resolved(subject_id, topic_id, result)
        chat.schedule(subject_id, topic_id)
        return result

    return router
