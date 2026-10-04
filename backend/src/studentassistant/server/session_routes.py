"""REST routes of protocol v1 for subjects, topics and the session lifecycle, under `/api`.

Thin: each route validates its body with the protocol model, calls the `SessionService` on
`app.state.sessions` and answers with the protocol model it returns. Lifecycle refusals map to
HTTP: an unknown subject, topic or session is 404, a clash with the current state (another session
active or unended, with code `session_open` -- see `server.errors` -- and its id in the
`X-Open-Session-Id` header; a session already ended; a vault pull that hit a conflict at session
start, whose detail names the conflicting paths) is 409, and a vault that cannot be opened is 503.
Optional fields are left out rather than sent as `null`, as the protocol's schemas want.
An end body's `prepare_notes` (protocol 1.6) is still accepted from old clients and ignored
(#440): ending a session never starts a notes generation.
`GET /api/sessions/{id}/health` (#262, web-only) answers the session's failure counts
(`session_health.py`): observer calls, page transcriptions, the vault's push streak, and whether the
observer is paused by a cost cap; every count 0 for a healthy session.
Every route sits behind the LAN guard, the Host allowlist and the bearer check.

These routes do not resolve the active user of a request yet, so they hand the `SessionService` no
user and it acts for the vault's single one (protocol 1.8's fallback); wiring them to
`server.user_scope.active_user_vault` is the next stage of #550.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from studentassistant import protocol
from studentassistant.protocol import PROTOCOL_VERSION, ErrorCode
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.auth import Principal
from studentassistant.server.errors import ApiError
from studentassistant.server.session_health import SessionHealth, SessionHealthResponse
from studentassistant.server.sessions import (
    ActiveSessionExistsError,
    OtherUserSessionOpenError,
    SessionConflictError,
    SessionService,
    UnknownSessionError,
    UnknownUserError,
    VaultUnavailableError,
)
from studentassistant.vault import SubjectNotFoundError, SyncStatus, TopicNotFoundError

# Path ids follow the protocol's id pattern, so `..` or a dotted name never reaches the vault.
SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
SessionId = Annotated[str, Path(pattern=ID_PATTERN)]


def _service(request: Request) -> SessionService:
    return request.app.state.sessions


def _principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


@asynccontextmanager
async def _http_errors() -> AsyncIterator[None]:
    try:
        yield
    except (
        UnknownSessionError,
        UnknownUserError,
        SubjectNotFoundError,
        TopicNotFoundError,
    ) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except ActiveSessionExistsError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            str(error),
            ErrorCode.SESSION_OPEN,
            headers={"X-Open-Session-Id": error.session_id},
        ) from error
    except OtherUserSessionOpenError as error:
        # 409 `session_open` too, but with NO `X-Open-Session-Id`: another user's session id is
        # never revealed, and the `detail` is the Spanish one `sessions.py` gives it (#550).
        raise ApiError(status.HTTP_409_CONFLICT, str(error), ErrorCode.SESSION_OPEN) from error
    except SessionConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except VaultUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error


def session_router() -> APIRouter:
    """The subjects/topics/sessions routes; the service is read from `app.state.sessions`."""
    router = APIRouter(prefix="/api")

    @router.get("/subjects", response_model_exclude_none=True)
    async def list_subjects(request: Request) -> protocol.SubjectsListResponse:
        async with _http_errors():
            return await _service(request).list_subjects(None)

    @router.post("/subjects", status_code=status.HTTP_201_CREATED, response_model_exclude_none=True)
    async def create_subject(
        request: Request, body: protocol.SubjectCreateRequest
    ) -> protocol.Subject:
        async with _http_errors():
            return await _service(request).create_subject(None, body.name)

    @router.get("/subjects/{subject_id}/topics", response_model_exclude_none=True)
    async def list_topics(request: Request, subject_id: SubjectId) -> protocol.TopicsListResponse:
        async with _http_errors():
            principal = _principal(request)
            client = principal.protocol_version if principal is not None else PROTOCOL_VERSION
            return await _service(request).list_topics(None, subject_id, protocol_version=client)

    @router.post(
        "/subjects/{subject_id}/topics",
        status_code=status.HTTP_201_CREATED,
        response_model_exclude_none=True,
    )
    async def create_topic(
        request: Request, subject_id: SubjectId, body: protocol.TopicCreateRequest
    ) -> protocol.Topic:
        async with _http_errors():
            return await _service(request).create_topic(None, subject_id, body.name)

    @router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model_exclude_none=True)
    async def start_session(
        request: Request, body: protocol.SessionStartRequest
    ) -> protocol.Session:
        async with _http_errors():
            return await _service(request).start(
                None,
                body.subject_id,
                body.topic_id,
                client_time_ms=body.client_time_ms,
                principal=_principal(request),
            )

    @router.post("/sessions/{session_id}/resume", response_model_exclude_none=True)
    async def resume_session(request: Request, session_id: SessionId) -> protocol.Session:
        async with _http_errors():
            return await _service(request).resume(None, session_id, principal=_principal(request))

    @router.post("/sessions/{session_id}/end", response_model_exclude_none=True)
    async def end_session(
        request: Request, session_id: SessionId, body: protocol.SessionEndRequest
    ) -> protocol.SessionEndResponse:
        # `body.prepare_notes` (old clients, #440) is deliberately not read.
        async with _http_errors():
            return await _service(request).end(
                session_id,
                client_time_ms=body.client_time_ms,
                reason=body.reason,
                principal=_principal(request),
            )

    @router.get("/sessions/{session_id}/health")
    async def session_health(request: Request, session_id: SessionId) -> SessionHealthResponse:
        service = _service(request)
        async with _http_errors():
            if not await service.is_known(None, session_id):
                raise UnknownSessionError(f"no existe la sesión {session_id}")
        health: SessionHealth = request.app.state.health
        sync = service.sync
        return health.summary(session_id, SyncStatus() if sync is None else sync.status())

    return router
