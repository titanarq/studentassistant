"""`DELETE /api/sources/{vault_id:path}`: the Recursos trash button retires one source (#451).

A soft delete ("retirar"): the vault's `remove_source` writes a `removed` mapping into the
source's sidecar and nothing is deleted. From then on the source is out of `list_sources`, so it
leaves the topic's source list and status (the Recursos tab), the selection a typed message may
send, the editor's catalogue and context, the observer's sources list, the transcription catch-up
and the search index; its files stay in the vault and in git history, and `GET /api/sources/...`
still serves it, so a footnote of the notes that cites it keeps working. The change is committed
at once (`Fuente <id> de <s>/<t> retirada`), and when the topic's session is live a
`source.removed` event (origin `user`, payload `source_id` topic-relative and `source_path`
vault-relative) is published on it; with no live session the sidecar and the commit are the
record.

204 on success. A path that is not a topic's `sources/<kind>/<file>`, or names no listed source
(never stored, a derived file or a sidecar, removed already) is 404 with the same Spanish `detail`
as the read routes; a sidecar that cannot be read 409; a vault that cannot be opened 503. The
route sits behind the LAN guard, the Host allowlist and the bearer check like every other.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from studentassistant.server.bus import BusError
from studentassistant.server.read_routes import UNKNOWN_SOURCE_DETAIL, VAULT_UNAVAILABLE_DETAIL
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    SourceFileError,
    SourceNotFoundError,
    SourcePathError,
    remove_source,
)

logger = logging.getLogger(__name__)

SOURCE_REMOVED_KIND = "source.removed"
"""The event published on the topic's live session when the student retires a source."""
UNREADABLE_SOURCE_DETAIL = "No se puede leer la ficha de esta fuente; no se ha retirado."


def source_router() -> APIRouter:
    """The source write routes; the vault comes from `app.state.sessions`."""
    router = APIRouter(prefix="/api")

    @router.delete(
        "/sources/{vault_id:path}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={
            404: {"description": "No listed source at that vault-relative path."},
            409: {"description": "The source's sidecar cannot be read."},
        },
    )
    async def delete_source(request: Request, vault_id: str) -> Response:
        service: SessionService = request.app.state.sessions
        try:
            vault = await service.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        try:
            await asyncio.to_thread(remove_source, vault, vault_id)
        except (SourcePathError, SourceNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_SOURCE_DETAIL) from error
        except SourceFileError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, UNREADABLE_SOURCE_DETAIL) from error
        # `remove_source` accepted it: `subjects/<s>/topics/<t>/sources/<kind>/<file>`.
        parts = vault_id.split("/")
        subject_id, topic_id = parts[1], parts[3]
        source_id = "/".join(parts[4:])
        sync = service.sync
        if sync is not None:
            sync.note_change()
            await asyncio.to_thread(
                sync.checkpoint, f"Fuente {source_id} de {subject_id}/{topic_id} retirada"
            )
        active = service.active
        if active is not None and (active.subject_id, active.topic_id) == (subject_id, topic_id):
            try:
                await request.app.state.bus.publish(
                    active.session_id,
                    SOURCE_REMOVED_KIND,
                    "user",
                    {"source_id": source_id, "source_path": vault_id},
                )
            except BusError:
                logger.info("the session of %s/%s ended meanwhile", subject_id, topic_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
