"""`GET /api/vault/status` and `GET /api/vault/divergence`: the vault's sync state, for the web.

What the student must know about keeping one vault on several PCs (ADR-0002), without anything
being blocked: pending commits and the last push failure, the last pull, another PC's open claim on
the vault (`host_warning`, `vault/active.py`) and a divergence git could not merge (`divergence`,
both sides kept, `vault/sync.py`). `GET /api/vault/divergence?path=` returns both versions of one
diverging path, so the student can compare them.

Asking for the status opens the vault if nothing had (which pulls it and reads the active-host
record); reading it afterwards runs no git. Web-only: not part of the phone protocol.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import ActiveHostWarning, Divergence, SyncStatus

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
NO_DIVERGENCE_DETAIL = "Esa ruta no está entre las que divergen."


class PushFailureView(BaseModel):
    kind: str
    message: str
    at: datetime


class LastSyncView(BaseModel):
    outcome: str
    message: str
    conflicts: list[str]
    at: datetime


class HostWarningView(BaseModel):
    host: str
    session_id: str | None
    subject: str | None
    topic: str | None
    claimed_at: datetime
    message: str


class DivergenceView(BaseModel):
    paths: list[str]
    local_commit: str
    remote_commit: str
    detected_at: datetime
    message: str


class VaultStatusResponse(BaseModel):
    host: str
    pending_changes: bool
    pending_commits: int
    last_commit_at: datetime | None
    last_push_at: datetime | None
    last_push_failure: PushFailureView | None
    last_sync: LastSyncView | None
    host_warning: HostWarningView | None
    divergence: DivergenceView | None


class DivergentVersionsResponse(BaseModel):
    path: str
    local: str | None
    remote: str | None


def _service(request: Request) -> SessionService:
    return request.app.state.sessions


async def _open(service: SessionService) -> None:
    try:
        await service.open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
        ) from error


def _host_warning(warning: ActiveHostWarning | None) -> HostWarningView | None:
    if warning is None:
        return None
    record = warning.record
    return HostWarningView(
        host=record.host,
        session_id=record.session_id,
        subject=record.subject,
        topic=record.topic,
        claimed_at=record.claimed_at,
        message=warning.message,
    )


def _divergence(divergence: Divergence | None) -> DivergenceView | None:
    if divergence is None:
        return None
    return DivergenceView(
        paths=list(divergence.paths),
        local_commit=divergence.local_commit,
        remote_commit=divergence.remote_commit,
        detected_at=divergence.detected_at,
        message=(
            "Este equipo y la copia de GitHub han cambiado los mismos archivos de forma"
            f" incompatible: {', '.join(divergence.paths)}. Se conservan las dos versiones;"
            " elige cuál quedarte antes de empezar otra sesión."
        ),
    )


def _response(service: SessionService, sync_status: SyncStatus) -> VaultStatusResponse:
    failure = sync_status.last_push_failure
    last_sync = sync_status.last_sync
    return VaultStatusResponse(
        host=service.host,
        pending_changes=sync_status.pending_changes,
        pending_commits=sync_status.pending_commits,
        last_commit_at=sync_status.last_commit_at,
        last_push_at=sync_status.last_push_at,
        last_push_failure=None
        if failure is None
        else PushFailureView(kind=failure.kind, message=failure.message, at=failure.at),
        last_sync=None
        if last_sync is None
        else LastSyncView(
            outcome=last_sync.outcome,
            message=last_sync.message,
            conflicts=list(last_sync.conflicts),
            at=last_sync.at,
        ),
        host_warning=_host_warning(service.host_warning),
        divergence=_divergence(sync_status.divergence),
    )


def vault_status_router() -> APIRouter:
    router = APIRouter(prefix="/api/vault")

    @router.get("/status")
    async def vault_status(request: Request) -> VaultStatusResponse:
        service = _service(request)
        await _open(service)
        sync = service.sync
        return _response(service, SyncStatus() if sync is None else sync.status())

    @router.get("/divergence")
    async def divergence(
        request: Request, path: str = Query(min_length=1)
    ) -> DivergentVersionsResponse:
        service = _service(request)
        await _open(service)
        sync = service.sync
        versions = None if sync is None else await asyncio.to_thread(sync.divergent_versions, path)
        if versions is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DIVERGENCE_DETAIL)
        return DivergentVersionsResponse(
            path=versions.path, local=versions.local, remote=versions.remote
        )

    return router
