"""The web's search route: `GET /api/search` over the vault's derived SQLite index.

Read-only and thin, like `read_routes.py`: the vault is the one `SessionService.open_vault()`
opened (and pulled), and the index is the `VaultIndex` the service opened next to it
(`SessionService.index`, kept current by its background loop). The query runs in a worker thread
through `VaultIndex.search`, the only reader of the index; nothing here reads a vault file.

The body is protocol `rest.search.response`. Errors, as `{"detail": "..."}` with a Spanish
`detail`: an unknown kind, a `topic` without its `subject`, an id outside the protocol's id
pattern or a `limit` out of range is 422; a vault that cannot be opened, or an index that could not
be, is 503. An unknown subject or topic is not an error: it just matches nothing. The route sits
behind the LAN guard, the Host allowlist and the bearer check like every other.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from studentassistant.protocol import SearchHit, SearchResponse
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.read_routes import VAULT_UNAVAILABLE_DETAIL
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault.index import DOC_KINDS
from studentassistant.vault.index import SearchHit as IndexHit

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 500

INDEX_UNAVAILABLE_DETAIL = "El índice de búsqueda no está disponible."
UNKNOWN_KIND_DETAIL = "Tipo de resultado desconocido: {kinds}. Los tipos son: {known}."
TOPIC_WITHOUT_SUBJECT_DETAIL = "Para filtrar por tema hay que indicar también la asignatura."


def parse_kinds(kinds: str | None) -> tuple[str, ...] | None:
    """`notes,transcript` as `("notes", "transcript")`; `None` (every kind) when absent or blank.

    Raises:
        ValueError: naming the kinds that are not one of `DOC_KINDS`.
    """
    if kinds is None:
        return None
    chosen = tuple(dict.fromkeys(part.strip() for part in kinds.split(",") if part.strip()))
    if not chosen:
        return None
    unknown = [kind for kind in chosen if kind not in DOC_KINDS]
    if unknown:
        raise ValueError(", ".join(unknown))
    return chosen


def _hit(hit: IndexHit) -> SearchHit:
    return SearchHit.model_validate(
        {
            "kind": hit.kind,
            "path": hit.path,
            "source": hit.source,
            "subject": hit.subject,
            "topic": hit.topic,
            "session": hit.session,
            "seq": hit.seq,
            "t_start": hit.t_start,
            "snippet": hit.snippet,
        }
    )


def search_router() -> APIRouter:
    """The web's search route; the vault and its index come from `app.state.sessions`."""
    router = APIRouter(prefix="/api")

    @router.get(
        "/search",
        response_model_exclude_none=True,
        responses={503: {"description": "The vault or its search index cannot be opened."}},
    )
    async def search(
        request: Request,
        q: Annotated[
            str,
            Query(
                max_length=MAX_QUERY_LENGTH,
                description="Plain text; every word must appear (as a prefix, accents ignored).",
            ),
        ],
        subject: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
        topic: Annotated[
            str | None, Query(pattern=ID_PATTERN, description="Needs `subject`.")
        ] = None,
        kinds: Annotated[
            str | None,
            Query(description=f"Comma-separated, of {', '.join(DOC_KINDS)}; default all."),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> SearchResponse:
        if topic is not None and subject is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, TOPIC_WITHOUT_SUBJECT_DETAIL)
        try:
            chosen = parse_kinds(kinds)
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                UNKNOWN_KIND_DETAIL.format(kinds=error, known=", ".join(DOC_KINDS)),
            ) from error
        service: SessionService = request.app.state.sessions
        try:
            await service.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        index = service.index
        if index is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, INDEX_UNAVAILABLE_DETAIL)
        hits = await asyncio.to_thread(
            index.search, q, subject=subject, topic=topic, kinds=chosen, limit=limit
        )
        return SearchResponse(query=q, hits=[_hit(hit) for hit in hits])

    return router
