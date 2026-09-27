"""The study workspace's live stream (SSE) and its typed chat messages.

`GET /api/subjects/{s}/topics/{t}/workspace/stream` is the live stream (below).
`POST /api/subjects/{s}/topics/{t}/workspace/messages` `{text}` -> 202 `{message_id, requests,
classified}`: a message typed in the workspace chat is classified by Sonnet like a spoken one
(`observer.requests.MessageClassifier`: the same prompt, tool and checks, the message as the
window) and each request is persisted as an `assistant.request` (origin `user`, `detector:
"typed"`, in the topic's live session or a review session) and queued in the request consumer
(`assistant_requests.py`), which runs it and streams the turn here. A message with no request, or
a classifier failure (`classified: false`), becomes one `edit`/`question` with the raw text, so
nothing typed is lost. `request_detection` (`wake_word`, `off`) is about speech: typed messages
are always classified. Errors: an empty or too long text 422, an unknown topic 404, a vault that
cannot be opened 503, a server without Claude 503. `POST .../notes/chat` stays for the old notes
page.

Everything that happens to the topic's chat and document while the stream is open, as the hub of
`server/workspace.py` delivers it (`request.detected`, `turn.started`, `reply.delta`,
`reply.restart`, `turn.result`, `turn.error`, `notes.changed`; the list is open). Every event's
`data` is one line of JSON. The stream starts with a `: connected` comment, then a
`: keep-alive` comment goes out every `KEEPALIVE_SECONDS` (15 s) without events, so a vanished
client is noticed; it ends only when the client goes away or the app shuts down. It works with or
without an active session and needs no `llm_transport` (a server without Claude has no turns, but
the student's saves and restores still stream). Nothing is replayed: a client that reconnects
reloads `GET .../notes/chat` and `GET .../notes`.

Errors before the stream, as `{"detail": "..."}` in Spanish: a vault that cannot be opened 503, an
unknown topic 404. The route sits behind the LAN guard, the Host allowlist and the bearer check.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from studentassistant.observer.requests import MessageClassifier
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.assistant_requests import AssistantRequestConsumer, TypedMessageResult
from studentassistant.server.revise_routes import sse
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.server.workspace import WorkspaceClosedError, WorkspaceHub
from studentassistant.vault import SubjectNotFoundError, TopicNotFoundError, get_topic

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

KEEPALIVE_SECONDS = 15.0
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
UNAVAILABLE_DETAIL = "El chat del espacio de estudio no está disponible: el servidor no usa Claude."
MAX_MESSAGE_CHARS = 4000


class WorkspaceMessage(BaseModel):
    """The body of `POST .../workspace/messages`."""

    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


CONNECTED = b": connected\n\n"
KEEPALIVE = b": keep-alive\n\n"


async def workspace_events(
    hub: WorkspaceHub,
    subject_id: str,
    topic_id: str,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """The topic's stream (see the module docstring); subscribes on the first iteration."""
    subscription = hub.subscribe(subject_id, topic_id)
    try:
        yield CONNECTED
        while True:
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=keepalive)
            except TimeoutError:
                yield KEEPALIVE
                continue
            except WorkspaceClosedError:
                return
            yield sse(event.event, dict(event.data))
    finally:
        subscription.close()


def workspace_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/subjects/{subject_id}/topics/{topic_id}/workspace/stream",
        response_class=StreamingResponse,
    )
    async def stream(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> StreamingResponse:
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        hub: WorkspaceHub = request.app.state.workspace
        keepalive: float = getattr(request.app.state, "workspace_keepalive", KEEPALIVE_SECONDS)
        return StreamingResponse(
            workspace_events(hub, subject_id, topic_id, keepalive=keepalive),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/workspace/messages", status_code=202)
    async def message(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: WorkspaceMessage
    ) -> TypedMessageResult:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="El mensaje está vacío.")
        consumer: AssistantRequestConsumer | None = request.app.state.assistant_requests
        if consumer is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        classifier: MessageClassifier | None = request.app.state.message_classifier
        return await consumer.post_message(subject_id, topic_id, body.text, classifier)

    return router


__all__ = ["KEEPALIVE_SECONDS", "workspace_events", "workspace_router"]
