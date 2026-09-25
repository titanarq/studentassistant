"""The voice tutor API: ask the tutor about a topic, and read the questions asked so far.

Thin: the work is `studentassistant.editor.tutor`. The vault is opened through the
`SessionService`, like the editor chat routes (`revise_routes.py`), whose stream shape it shares:

- `POST /api/subjects/{s}/topics/{t}/tutor` (`TutorRequest`: `question`, `confirm_over_cap`)
  answers with a Server-Sent Events stream: the answer as it is written (`reply.delta`), then one
  `result` event with the `TutorAnswer` (`question`, `reply`, `refs`, `warning`) or one `error`
  event (`status`, `detail`, and `code` when the failure has one and the caller speaks it). The
  question runs as its own task, so a client that goes away does not lose the recorded answer.
  It only reads the notes, so it does not take the notes lock of "prepárame el tema", the chat and
  the doubts; one question per topic runs at a time (its own lock).
- `GET /api/subjects/{s}/topics/{t}/tutor` -> `TutorHistory`.

Errors before the stream are HTTP errors, `{"detail": "..."}` in Spanish: no `llm_transport` 503,
a vault that cannot be opened 503, an unknown topic 404, no notes yet or another question of the
topic running 409, an empty question 422. In the stream, the `error` event carries the status the
same failure would have had: a reached cost cap 409 `cost_cap_reached` (until the body says
`confirm_over_cap`), a Claude refusal or failure 502.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from studentassistant.editor.revise import InvalidMessageError, RevisionError
from studentassistant.editor.tutor import (
    MAX_QUESTION_CHARS,
    TutorHistory,
    ask_tutor,
    tutor_history,
)
from studentassistant.llm import (
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    get_client,
)
from studentassistant.protocol import ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.errors import caller_speaks_error_codes, cost_cap_error
from studentassistant.server.notes_routes import NotesGenerator
from studentassistant.server.revise_routes import sse
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    GitSync,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
    read_notes,
)

logger = logging.getLogger(__name__)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

UNAVAILABLE_DETAIL = "El tutor no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "El tutor ya está contestando otra pregunta de este tema."
NO_NOTES_DETAIL = "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."
EMPTY_DETAIL = "Dime qué quieres preguntar."
REFUSED_DETAIL = "Claude se ha negado a contestar esa pregunta."
FAILED_DETAIL = "El tutor no ha podido contestar: Claude no ha respondido. Prueba más tarde."
INTERNAL_DETAIL = "El tutor no ha podido contestar por un error del servidor."
CAP_THEN = "Confirma para continuar igualmente."


class TutorRequest(BaseModel):
    """One question of the student; `confirm_over_cap` proceeds past a reached cost cap."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    confirm_over_cap: bool = False


def _error_of(error: BaseException) -> tuple[int, str, ErrorCode | None]:
    if isinstance(error, InvalidMessageError):
        return 422, str(error), None
    if isinstance(error, RevisionError):
        return 409, str(error), None
    if isinstance(error, CostConfirmationRequiredError):
        refused = cost_cap_error(error, CAP_THEN)
        return refused.status_code, refused.detail, refused.code
    if isinstance(error, RefusalError):
        return 502, REFUSED_DETAIL, None
    if isinstance(error, LLMError):
        return 502, FAILED_DETAIL, None
    return 500, INTERNAL_DETAIL, None


def tutor_router() -> APIRouter:
    router = APIRouter()
    running: set[tuple[str, str]] = set()
    # The running questions, so one whose client went away is not garbage-collected.
    tasks: set[asyncio.Task[None]] = set()

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

    @router.get("/api/subjects/{subject_id}/topics/{topic_id}/tutor")
    async def history(request: Request, subject_id: SubjectId, topic_id: TopicId) -> TutorHistory:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(tutor_history, vault, subject_id, topic_id)

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/tutor")
    async def ask(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: TutorRequest
    ) -> StreamingResponse:
        generator: NotesGenerator | None = request.app.state.notes
        if generator is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        vault, sync = await open_topic(request, subject_id, topic_id)
        if not body.question.strip():
            raise HTTPException(status_code=422, detail=EMPTY_DETAIL)
        notes = await asyncio.to_thread(read_notes, vault, subject_id, topic_id)
        if not notes or not notes.strip():
            raise HTTPException(status_code=409, detail=NO_NOTES_DETAIL)
        key = (subject_id, topic_id)
        if key in running:
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        running.add(key)

        speaks_codes = caller_speaks_error_codes(request)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def on_reply(kind: str, data: dict[str, Any]) -> None:
            queue.put_nowait(sse(kind, data))

        async def run() -> None:
            try:
                client = get_client(
                    "editor",
                    settings=generator.settings,
                    transport=generator.transport,
                    ledger=LedgerBinding(vault, subject_id, topic_id),
                )
                answer = await ask_tutor(
                    vault,
                    subject_id,
                    topic_id,
                    body.question,
                    client=client,
                    sync=sync,
                    on_reply=on_reply,
                    confirm_over_cap=body.confirm_over_cap,
                )
                queue.put_nowait(sse("result", answer.model_dump(mode="json")))
            except Exception as error:
                status, detail, code = _error_of(error)
                if status == 500:
                    logger.exception("the tutor of %s/%s failed", subject_id, topic_id)
                elif status == 502:
                    logger.warning("the tutor of %s/%s failed: %s", subject_id, topic_id, error)
                data: dict[str, Any] = {"status": status, "detail": detail}
                if code is not None and speaks_codes:
                    data["code"] = code.value
                queue.put_nowait(sse("error", data))
            finally:
                running.discard(key)
                queue.put_nowait(None)

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        async def stream() -> AsyncIterator[bytes]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
