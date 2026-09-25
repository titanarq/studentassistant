"""The editor chat API: revise the notes in conversation, read the conversation, undo a turn.

Thin: the work is `studentassistant.editor.revise`. Every route opens the vault through the
`SessionService` (the pulled vault and its `GitSync`), like the notes and doubts routes.

- `POST .../notes/chat` answers with a Server-Sent Events stream (`text/event-stream`): the
  editor's reply as it is written (`reply.delta`, `reply.restart` on a re-ask), then one `result`
  event with the `RevisionResult` (the applied diff) or one `error` event (`status`, `detail`,
  and `code` when the failure has one and the caller speaks it, as in a REST error body).
  The turn runs as its own task, so a client that goes away does not cut a change in half; it
  holds the topic's notes lock (`NotesGenerator.claim`) with "prepárame el tema" and the doubts.
- `GET .../notes/chat` -> `ChatHistory`; `POST .../notes/chat/undo` -> `UndoResult` (no Claude
  call, but the notes lock too).

Errors before the stream starts are ordinary HTTP errors, as `{"detail": "..."}` in Spanish: no
`llm_transport` 503 (chat only), a vault that cannot be opened 503, an unknown topic 404, another
notes operation of the topic running or no notes yet 409, an invalid message 422; undo: nothing to
undo or a later change in the way 409. Inside the stream, the `error` event carries the status the
same failure would have had: a reached cost cap 409 `cost_cap_reached` (until the body says
`confirm_over_cap`), a Claude refusal or failure 502.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from studentassistant.editor.revise import (
    MAX_MESSAGE_CHARS,
    ChatHistory,
    InvalidMessageError,
    RevisionError,
    UndoResult,
    chat_history,
    revise_notes,
    undo_last_revision,
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

UNAVAILABLE_DETAIL = "El chat con el editor no está disponible: el servidor no usa Claude."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "El editor ya está trabajando en los apuntes o las dudas de este tema."
REFUSED_DETAIL = "Claude se ha negado a revisar los apuntes de este tema."
FAILED_DETAIL = "No se han podido revisar los apuntes: Claude no ha respondido. Prueba más tarde."
NO_NOTES_DETAIL = "Todavía no hay apuntes de este tema: prepáralos antes de revisarlos."
INTERNAL_DETAIL = "No se han podido revisar los apuntes por un error del servidor."


CAP_THEN = "Confirma para continuar igualmente."


class ChatRequest(BaseModel):
    """One message of the student; `confirm_over_cap` proceeds past a reached cost cap."""

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    confirm_over_cap: bool = False


def sse(event: str, data: dict[str, Any]) -> bytes:
    """One Server-Sent Event: `event:` and a one-line JSON `data:`."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


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


def revise_router() -> APIRouter:
    router = APIRouter()
    # The running turns, so a turn whose client went away is not garbage-collected.
    turns: set[asyncio.Task[None]] = set()

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

    def publisher(request: Request, subject_id: str, topic_id: str) -> Any:
        sessions: SessionService = request.app.state.sessions

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await sessions.bus.publish(active.session_id, kind, "editor", payload)

        return publish

    @router.get("/api/subjects/{subject_id}/topics/{topic_id}/notes/chat")
    async def history(request: Request, subject_id: SubjectId, topic_id: TopicId) -> ChatHistory:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(chat_history, vault, subject_id, topic_id)

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/notes/chat")
    async def chat(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: ChatRequest
    ) -> StreamingResponse:
        generator: NotesGenerator | None = request.app.state.notes
        if generator is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        vault, sync = await open_topic(request, subject_id, topic_id)
        if not body.message.strip():
            raise HTTPException(status_code=422, detail="Escribe qué quieres cambiar.")
        notes = await asyncio.to_thread(read_notes, vault, subject_id, topic_id)
        if not notes or not notes.strip():
            raise HTTPException(status_code=409, detail=NO_NOTES_DETAIL)
        if not generator.claim(subject_id, topic_id):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)

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
                result = await revise_notes(
                    vault,
                    subject_id,
                    topic_id,
                    body.message,
                    client=client,
                    sync=sync,
                    on_reply=on_reply,
                    on_event=publisher(request, subject_id, topic_id),
                    confirm_over_cap=body.confirm_over_cap,
                )
                queue.put_nowait(sse("result", result.model_dump(mode="json")))
            except Exception as error:
                status, detail, code = _error_of(error)
                if status == 500:
                    logger.exception("the notes chat of %s/%s failed", subject_id, topic_id)
                elif status == 502:
                    logger.warning(
                        "the notes chat of %s/%s failed: %s", subject_id, topic_id, error
                    )
                data: dict[str, Any] = {"status": status, "detail": detail}
                if code is not None and speaks_codes:
                    data["code"] = code.value
                queue.put_nowait(sse("error", data))
            finally:
                generator.release(subject_id, topic_id)
                queue.put_nowait(None)

        task = asyncio.create_task(run())
        turns.add(task)
        task.add_done_callback(turns.discard)

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

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/notes/chat/undo")
    async def undo(request: Request, subject_id: SubjectId, topic_id: TopicId) -> UndoResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        generator: NotesGenerator | None = request.app.state.notes
        if generator is not None and not generator.claim(subject_id, topic_id):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        try:
            return await undo_last_revision(
                vault,
                subject_id,
                topic_id,
                sync=sync,
                on_event=publisher(request, subject_id, topic_id),
            )
        except RevisionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            if generator is not None:
                generator.release(subject_id, topic_id)

    return router
