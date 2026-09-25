"""The web search API of the review UI: queue a search, list a topic's searches, keep a result.

Thin: the work is `studentassistant.sources.web_searcher.WebSearcher` (`app.state.web_searcher`,
built when the app has an `llm_transport` and `[sources] web_search_enabled`).

- `GET /api/subjects/{s}/topics/{t}/web-searches` -> `{"searches": [...]}`, newest first: each
  search's `search_id`, `query`, `requested_by` (`voice` | `web` | `editor`), `session_id`,
  `queued_at`, `status` (`queued` | `done` | `failed`), `results` (`url`, `title`, `summary`,
  `relevant`, `found_in_search`), `reason`/`message` of a failure, and the `kept` results
  (`index`, `url`, `source_id`, `kept_by`). Listing works without a transport too.
- `POST /api/subjects/{s}/topics/{t}/web-searches` with `{"query": "..."}` -> 202
  `{"search_id", "status": "queued"}`; the search runs in the background (poll the list). When the
  active session is of the same topic, the search is bound to it (its cost ledger and its events).
- `POST .../web-searches/{search_id}/results/{index}/keep` -> 201 `{"source_id", "vault_id",
  "title", "url"}` once the page is fetched and stored as `sources/web/NNN-<slug>.md`; keeping it
  again answers the first snapshot.

Errors, as `{"detail": "..."}` in Spanish: an unknown topic, search or result 404; a search with
no results yet or a reached cost cap 409; an empty query, or a page that cannot be kept as text
(a PDF, an error page, a page that looks like it carries a key) 422; a Claude failure or refusal
502; no web search (no transport, or disabled) or a vault that cannot be opened 503.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from studentassistant.llm import CostCapError, LLMError, RefusalError
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.sources.web import WebSearchRecord, list_web_searches
from studentassistant.sources.web_searcher import KeepError, WebSearcher
from studentassistant.vault import SubjectNotFoundError, TopicNotFoundError, Vault, get_topic

SubjectId = Annotated[str, PathParam(pattern=ID_PATTERN)]
TopicId = Annotated[str, PathParam(pattern=ID_PATTERN)]
SearchId = Annotated[str, PathParam(pattern=ID_PATTERN)]

UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNAVAILABLE_DETAIL = "La búsqueda en Internet no está disponible en este servidor."
EMPTY_QUERY_DETAIL = "Escribe qué quieres buscar."
COST_CAP_DETAIL = "Se ha alcanzado el límite de gasto: no se puede usar Claude ahora."
CLAUDE_FAILED_DETAIL = "Claude no ha podido descargar la página. Inténtalo de nuevo."
REFUSED_DETAIL = "Claude se ha negado a descargar esa página."
MAX_QUERY_CHARS = 500


class WebSearchRequest(BaseModel):
    query: str = Field(max_length=MAX_QUERY_CHARS)


class WebSearchQueued(BaseModel):
    search_id: str
    status: str = "queued"


class WebSearchList(BaseModel):
    searches: list[WebSearchRecord]


class KeptResponse(BaseModel):
    source_id: str = Field(description="`sources/web/NNN-<slug>.md`, as provenance cites it.")
    vault_id: str = Field(description="The stored snapshot's vault-relative path.")
    title: str
    url: str


async def _vault(request: Request) -> Vault:
    service: SessionService = request.app.state.sessions
    try:
        return await service.open_vault()
    except VaultUnavailableError as error:
        unavailable = status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(unavailable, VAULT_UNAVAILABLE_DETAIL) from error


async def _topic_vault(request: Request, subject_id: str, topic_id: str) -> Vault:
    vault = await _vault(request)
    try:
        await asyncio.to_thread(get_topic, vault, subject_id, topic_id)
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
    return vault


def _searcher(request: Request) -> WebSearcher:
    searcher: WebSearcher | None = request.app.state.web_searcher
    if searcher is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE_DETAIL)
    return searcher


def _topic_session(request: Request, subject_id: str, topic_id: str) -> str | None:
    service: SessionService = request.app.state.sessions
    active = service.active
    if active is not None and (active.subject_id, active.topic_id) == (subject_id, topic_id):
        return active.session_id
    return None


def web_search_router() -> APIRouter:
    router = APIRouter(prefix="/api/subjects/{subject_id}/topics/{topic_id}/web-searches")

    @router.get("")
    async def list_searches(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> WebSearchList:
        vault = await _topic_vault(request, subject_id, topic_id)
        searcher: WebSearcher | None = request.app.state.web_searcher
        if searcher is None:
            searches = await asyncio.to_thread(list_web_searches, vault, subject_id, topic_id)
        else:
            searches = await searcher.list(vault, subject_id, topic_id)
        return WebSearchList(searches=searches)

    @router.post("", status_code=status.HTTP_202_ACCEPTED)
    async def queue_search(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: WebSearchRequest
    ) -> WebSearchQueued:
        searcher = _searcher(request)
        if not body.query.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_QUERY_DETAIL)
        vault = await _topic_vault(request, subject_id, topic_id)
        search_id = await searcher.submit(
            vault,
            subject_id,
            topic_id,
            body.query,
            requested_by="web",
            session_id=_topic_session(request, subject_id, topic_id),
        )
        return WebSearchQueued(search_id=search_id)

    @router.post("/{search_id}/results/{index}/keep", status_code=status.HTTP_201_CREATED)
    async def keep_result(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        search_id: SearchId,
        index: Annotated[int, PathParam(ge=0, le=100)],
    ) -> KeptResponse:
        searcher = _searcher(request)
        vault = await _topic_vault(request, subject_id, topic_id)
        try:
            kept = await searcher.keep(
                vault,
                subject_id,
                topic_id,
                search_id,
                index,
                kept_by="student",
                session_id=_topic_session(request, subject_id, topic_id),
            )
        except KeepError as error:
            raise HTTPException(error.status, str(error)) from error
        except CostCapError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, COST_CAP_DETAIL) from error
        except RefusalError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, REFUSED_DETAIL) from error
        except LLMError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, CLAUDE_FAILED_DETAIL) from error
        return KeptResponse(
            source_id=kept.source_id,
            vault_id=kept.path.resolve().relative_to(vault.path.resolve()).as_posix(),
            title=kept.title,
            url=kept.url,
        )

    return router


__all__ = ["KeptResponse", "WebSearchList", "WebSearchQueued", "web_search_router"]
