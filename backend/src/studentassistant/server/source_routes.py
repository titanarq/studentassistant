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

`PUT /api/sources/{vault_id:path}/transcription` (#473): the student's hand correction of a
photographed page's transcription (Recursos' detail view). Body `{"text": "..."}`; the vault's
`edit_page_transcription` writes it as the page's `page-NNN.md` and records
`transcription_edited: {at, by: student, previous_sha256}` in the sidecar (the replaced text stays
in git history). Committed at once (`Transcripción de <id> de <s>/<t> corregida`); when the topic's
session is live a `page.transcription_edited` event (origin `user`, payload `source_id`,
`source_path`, `transcription_path`) is published on it. 200 with `{source_path,
transcription_path, text}` (the text as stored). 404 like `DELETE` for anything that is not a
listed `notes`/`book` page; 409 when the page has no transcription yet (nothing to correct) or its
sidecar cannot be read; 422 for a blank text or one the secret guard refuses; 503 without a vault.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from studentassistant.server.bus import BusError
from studentassistant.server.read_routes import UNKNOWN_SOURCE_DETAIL, VAULT_UNAVAILABLE_DETAIL
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    NoTranscriptionError,
    SecretRefused,
    SourceFileError,
    SourceNotFoundError,
    SourcePathError,
    edit_page_transcription,
    remove_source,
)

logger = logging.getLogger(__name__)

SOURCE_REMOVED_KIND = "source.removed"
"""The event published on the topic's live session when the student retires a source."""
UNREADABLE_SOURCE_DETAIL = "No se puede leer la ficha de esta fuente; no se ha retirado."
TRANSCRIPTION_EDITED_KIND = "page.transcription_edited"
"""The event published on the topic's live session when the student corrects a transcription."""
NOT_TRANSCRIBED_DETAIL = "Esta página todavía no está transcrita; no hay nada que corregir."
UNREADABLE_PAGE_DETAIL = "No se puede leer la ficha de esta página; no se ha guardado."
BLANK_TRANSCRIPTION_DETAIL = "La transcripción no puede quedar vacía."
SECRET_TRANSCRIPTION_DETAIL = "El texto parece contener una clave o un secreto; no se ha guardado."


class TranscriptionEdit(BaseModel):
    """`PUT /api/sources/{vault_id}/transcription`: the student's corrected transcription."""

    text: str = Field(
        max_length=200_000, description="The page's Markdown as the student wants it."
    )


class EditedTranscription(BaseModel):
    """What the correction stored."""

    source_path: str
    transcription_path: str
    text: str


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

    @router.put(
        "/sources/{vault_id:path}/transcription",
        responses={
            404: {"description": "No listed notes or book page at that vault-relative path."},
            409: {
                "description": "The page has no transcription yet, or its sidecar is unreadable."
            },
            422: {"description": "A blank text, or one the secret guard refuses."},
        },
    )
    async def edit_transcription(
        request: Request, vault_id: str, body: TranscriptionEdit
    ) -> EditedTranscription:
        service: SessionService = request.app.state.sessions
        try:
            vault = await service.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        try:
            written = await asyncio.to_thread(edit_page_transcription, vault, vault_id, body.text)
        except (SourcePathError, SourceNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_SOURCE_DETAIL) from error
        except NoTranscriptionError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, NOT_TRANSCRIBED_DETAIL) from error
        except SourceFileError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, UNREADABLE_PAGE_DETAIL) from error
        except SecretRefused as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, SECRET_TRANSCRIPTION_DETAIL
            ) from error
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, BLANK_TRANSCRIPTION_DETAIL
            ) from error
        parts = vault_id.split("/")
        subject_id, topic_id = parts[1], parts[3]
        source_id = "/".join(parts[4:])
        transcription_path = written.relative_to(vault.path).as_posix()
        sync = service.sync
        if sync is not None:
            sync.note_change()
            await asyncio.to_thread(
                sync.checkpoint,
                f"Transcripción de {source_id} de {subject_id}/{topic_id} corregida",
            )
        active = service.active
        if active is not None and (active.subject_id, active.topic_id) == (subject_id, topic_id):
            try:
                await request.app.state.bus.publish(
                    active.session_id,
                    TRANSCRIPTION_EDITED_KIND,
                    "user",
                    {
                        "source_id": source_id,
                        "source_path": vault_id,
                        "transcription_path": transcription_path,
                    },
                )
            except BusError:
                logger.info("the session of %s/%s ended meanwhile", subject_id, topic_id)
        text = await asyncio.to_thread(written.read_text, encoding="utf-8")
        return EditedTranscription(
            source_path=vault_id, transcription_path=transcription_path, text=text
        )

    return router
