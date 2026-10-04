"""The study workspace's live stream (SSE) and its typed chat messages.

`GET /api/subjects/{s}/topics/{t}/workspace/stream` is the live stream (below).
`POST /api/subjects/{s}/topics/{t}/workspace/messages` `{text, selected_source_ids?}` -> 202
`{message_id, requests, classified}`: a message typed in the workspace chat is classified by Sonnet
like a spoken one (`observer.requests.MessageClassifier`: the same prompt, tool and checks, the
message as the window) and each request is persisted as an `assistant.request` (origin `user`,
`detector: "typed"`, in the topic's live session or a review session) and queued in the request
consumer (`assistant_requests.py`), which runs it and streams the turn here. A message with no
request, or a classifier failure (`classified: false`), becomes one `edit`/`question` with the raw
text, so nothing typed is lost. `request_detection` (`wake_word`, `off`) is about speech: typed
messages are always classified. Errors: an empty or too long text 422, an unknown topic 404, a vault
that cannot be opened 503, a server without Claude 503. `POST .../notes/chat` stays for the old
notes page.

`selected_source_ids` (#433, optional) is what the student has selected in the Recursos tab, in
order: topic-relative source ids (`sources/notes/page-003.jpg`; a PDF page as `<pdf>#page=K`,
kept so), at most `[observer] max_selected_sources` (default 20). It is the referent of «esto» /
«estas páginas» for the classifier, kept on every request of the message
(`AssistantRequest.selected_source_ids`), echoed by `request.detected`, and the selected pages go
first in an `edit`/`question` turn. More ids than the limit, or one that is not a source of the
topic, is a 422 with a Spanish `detail`; it is not allowed with `confirm_over_cap` (422). Without
it, a message behaves as before.

`{confirm_over_cap: true, turn_id}` (no `text`, #351) is "Continuar igualmente" on a turn of the
stream that stopped at the cost cap (`turn.error` `cost_cap_reached`): the request that turn ran
is queued again as it was classified, confirmed past the cap (`AssistantRequestConsumer.confirm`),
and answered 202 with that one request; its new turn streams here with the same `request_id`.
Any request kind (`edit`, `question`, `prepare_notes`, `incorporate`, `doubt_answer`) is
confirmed this way. A `turn_id` that is not such a turn (never was, already confirmed, or
forgotten after a restart) 404; `text` together with `turn_id`, or `turn_id` without
`confirm_over_cap`, 422.

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
from pydantic import BaseModel, Field, model_validator

from studentassistant.observer.requests import MessageClassifier
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.assistant_requests import (
    NOT_STOPPED_DETAIL,
    AssistantRequestConsumer,
    NotStoppedError,
    SelectionError,
    TypedMessageResult,
    check_selection,
)
from studentassistant.server.revise_routes import sse
from studentassistant.server.serving import until_shutdown
from studentassistant.server.user_scope import active_user_vault
from studentassistant.server.workspace import WorkspaceClosedError, WorkspaceHub
from studentassistant.vault import SubjectNotFoundError, TopicNotFoundError, get_topic

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]

KEEPALIVE_SECONDS = 15.0
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
UNAVAILABLE_DETAIL = "El chat del espacio de estudio no está disponible: el servidor no usa Claude."
MAX_MESSAGE_CHARS = 4000
MAX_SELECTED_IDS = 200
"""A hard bound on the body's selection; the configured limit (`[observer]
max_selected_sources`, default 20) is checked in the route."""


class WorkspaceMessage(BaseModel):
    """The body of `POST .../workspace/messages`: a typed `text`, or the confirmation past the
    cost cap of the request whose turn `turn_id` stopped there (`confirm_over_cap`)."""

    text: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)
    confirm_over_cap: bool = False
    turn_id: str | None = Field(default=None, min_length=1, max_length=64)
    selected_source_ids: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(
        default=None, max_length=MAX_SELECTED_IDS
    )

    @model_validator(mode="after")
    def _one_of(self) -> WorkspaceMessage:
        if self.selected_source_ids is not None and self.confirm_over_cap:
            raise ValueError("a confirmation carries no selected_source_ids")
        if self.turn_id is not None:
            if self.text is not None:
                raise ValueError("a confirmation carries a turn_id and no text")
            if not self.confirm_over_cap:
                raise ValueError("a turn_id is only sent with confirm_over_cap")
        elif self.text is None:
            raise ValueError("text is required")
        return self


CONNECTED = b": connected\n\n"
KEEPALIVE = b": keep-alive\n\n"


async def workspace_events(
    hub: WorkspaceHub,
    subject_id: str,
    topic_id: str,
    *,
    user_id: str | None = None,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """The user's topic stream (see the module docstring); subscribes on the first iteration."""
    subscription = hub.subscribe(subject_id, topic_id, user_id=user_id)
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
        vault, _ = await active_user_vault(request)
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        user_id = vault.user_id
        hub: WorkspaceHub = request.app.state.workspace
        keepalive: float = getattr(request.app.state, "workspace_keepalive", KEEPALIVE_SECONDS)
        return StreamingResponse(
            until_shutdown(
                workspace_events(hub, subject_id, topic_id, user_id=user_id, keepalive=keepalive),
                request.app.state.shutdown,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/subjects/{subject_id}/topics/{topic_id}/workspace/messages", status_code=202)
    async def message(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: WorkspaceMessage
    ) -> TypedMessageResult:
        if body.turn_id is None and not (body.text or "").strip():
            raise HTTPException(status_code=422, detail="El mensaje está vacío.")
        consumer: AssistantRequestConsumer | None = request.app.state.assistant_requests
        if consumer is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        vault, _ = await active_user_vault(request)
        try:
            await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
        if body.turn_id is not None:
            try:
                return consumer.confirm(subject_id, topic_id, body.turn_id, user_id=vault.user_id)
            except NotStoppedError as error:
                raise HTTPException(status_code=404, detail=NOT_STOPPED_DETAIL) from error
        selected: list[str] = []
        if body.selected_source_ids:
            limit = consumer.generator.settings.observer.max_selected_sources
            try:
                selected = await asyncio.to_thread(
                    check_selection, vault, subject_id, topic_id, body.selected_source_ids, limit
                )
            except SelectionError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        classifier: MessageClassifier | None = request.app.state.message_classifier
        return await consumer.post_message(
            subject_id,
            topic_id,
            body.text or "",
            classifier,
            selected=selected,
            user_id=vault.user_id,
        )

    return router


__all__ = ["KEEPALIVE_SECONDS", "workspace_events", "workspace_router"]
