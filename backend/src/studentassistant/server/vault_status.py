"""`GET /api/vault/status` and `GET /api/vault/divergence`: the vault's sync state, for the web.

What the student must know about keeping one vault on several PCs (ADR-0002), without anything
being blocked: pending commits and the last push failure, the last pull, another PC's open claim on
the vault (`host_warning`, `vault/active.py`) and a divergence git could not merge (`divergence`,
both sides kept, `vault/sync.py`). `GET /api/vault/divergence?path=` returns both versions of one
diverging path, so the student can compare them.

Asking for the status opens the vault if nothing had (which pulls it and reads the active-host
record); reading it afterwards runs no git. Web-only: not part of the phone protocol.

Both routes are user-scoped (protocol 1.8, #550): they declare
`server.user_scope.active_user_vault`, so a browser that has not picked a user on a vault holding
several is refused with 400 `user_required` here as it is on every content route, and the other
PC's open claim they report names the student capturing on it -- on a shared vault, "another PC is
capturing" is only half of what a student needs to decide whether to wait (`vault/active.py`). What
they report is the REPOSITORY's state, though: one git, one set of pending commits, one divergence,
one claim, and the paths a divergence lists are git's own (`users/<id>/...`), which is why the
versions of a diverging path are read through the repository's sync and not through the user's view
of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from studentassistant.server.sessions import SessionService
from studentassistant.server.user_scope import UserScope
from studentassistant.vault import ActiveHostWarning, Divergence, SyncStatus

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
    # The student the other PC is capturing as, `None` in a claim a backend of #547 or earlier
    # wrote: on a vault several students share, the host alone does not say whether to wait.
    user: str | None
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


def _host_warning(warning: ActiveHostWarning | None) -> HostWarningView | None:
    if warning is None:
        return None
    record = warning.record
    return HostWarningView(
        host=record.host,
        user=record.user,
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
    async def vault_status(request: Request, scope: UserScope) -> VaultStatusResponse:
        # `scope` is the route's user resolution, not a handle it reads: what it reports is the
        # repository's own state, and the dependency opening the vault is what makes the sync and
        # the active-host record there to report (a vault it cannot open is its own 503).
        service = _service(request)
        sync = service.sync
        return _response(service, SyncStatus() if sync is None else sync.status())

    @router.get("/divergence")
    async def divergence(
        request: Request, scope: UserScope, path: str = Query(min_length=1)
    ) -> DivergentVersionsResponse:
        service = _service(request)
        sync = service.sync
        versions = None if sync is None else await asyncio.to_thread(sync.divergent_versions, path)
        if versions is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DIVERGENCE_DETAIL)
        return DivergentVersionsResponse(
            path=versions.path, local=versions.local, remote=versions.remote
        )

    return router
