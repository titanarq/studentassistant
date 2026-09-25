"""The notes versions API: list the tagged versions, read one, diff two by section, restore one.

Thin: the work is `studentassistant.editor.versions`. Every route opens the vault through the
`SessionService` (the pulled vault and its `GitSync`), like the other notes routes. No route calls
Claude, so they all work without an `llm_transport`.

- `GET .../notes/versions` -> `NotesVersions`.
- `GET .../notes/versions/diff?from=1&to=2` -> `VersionDiff`; without `to`, against the current
  `apuntes.md`.
- `GET .../notes/versions/{version}` -> `VersionText`.
- `POST .../notes/versions/{version}/restore`, no body -> `RestoreResult` (a new commit and the
  next `apuntes-vN` tag). It holds the topic's notes lock (`NotesGenerator.claim`) like the
  generation, the chat and the doubts; `notes.restored` is published on the bus (origin
  `editor`) when the topic's session is the active one.

Errors, as `{"detail": "..."}` in Spanish: a vault that cannot be opened 503, an unknown topic or
version 404, nothing to compare with, the current notes already being that version, or another
notes operation of the topic running 409.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request

from studentassistant.editor.versions import (
    NotesVersions,
    RestoreResult,
    UnknownVersionError,
    VersionDiff,
    VersionError,
    VersionText,
    diff_versions,
    list_versions,
    read_version,
    restore_version,
)
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.notes_routes import NotesGenerator
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    GitSync,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_topic,
)

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]
Version = Annotated[int, Path(ge=1)]

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
BUSY_DETAIL = "El editor ya está trabajando en los apuntes o las dudas de este tema."

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/notes/versions"


def _http_error(error: VersionError) -> HTTPException:
    status = 404 if isinstance(error, UnknownVersionError) else 409
    return HTTPException(status_code=status, detail=str(error))


def versions_router() -> APIRouter:
    router = APIRouter()

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

    @router.get(BASE)
    async def versions(request: Request, subject_id: SubjectId, topic_id: TopicId) -> NotesVersions:
        vault, sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(list_versions, vault, subject_id, topic_id, sync=sync)

    @router.get(BASE + "/diff")
    async def diff(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        from_version: Annotated[int, Query(alias="from", ge=1)],
        to_version: Annotated[int | None, Query(alias="to", ge=1)] = None,
    ) -> VersionDiff:
        vault, sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                diff_versions, vault, subject_id, topic_id, from_version, to_version, sync=sync
            )
        except VersionError as error:
            raise _http_error(error) from error

    @router.get(BASE + "/{version}")
    async def version_text(
        request: Request, subject_id: SubjectId, topic_id: TopicId, version: Version
    ) -> VersionText:
        vault, sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                read_version, vault, subject_id, topic_id, version, sync=sync
            )
        except VersionError as error:
            raise _http_error(error) from error

    @router.post(BASE + "/{version}/restore")
    async def restore(
        request: Request, subject_id: SubjectId, topic_id: TopicId, version: Version
    ) -> RestoreResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        generator: NotesGenerator | None = request.app.state.notes
        if generator is not None and not generator.claim(subject_id, topic_id):
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        sessions: SessionService = request.app.state.sessions

        async def publish(kind: str, payload: dict[str, Any]) -> None:
            active = sessions.active
            if active is not None and (active.subject_id, active.topic_id) == (
                subject_id,
                topic_id,
            ):
                await sessions.bus.publish(active.session_id, kind, "editor", payload)

        try:
            return await restore_version(
                vault, subject_id, topic_id, version, sync=sync, on_event=publish
            )
        except VersionError as error:
            raise _http_error(error) from error
        finally:
            if generator is not None:
                generator.release(subject_id, topic_id)

    return router
