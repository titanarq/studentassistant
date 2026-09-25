"""`GET`/`PUT /api/subjects/{subject_id}/topics/{topic_id}/book`: the topic's textbook (#58).

A topic's `book` pages (textbook pages shown to the camera) come from one book; its title is kept
in `sources/book/book.yaml` (`vault.set_book`), named in each book page's transcription request
and in how the editor cites a book page (`Libro «<título>», página 83`). `GET` answers the title
(`null` when none was set); `PUT` with `{"title": "..."}` records it (spaces collapsed) and tells
the vault's sync a file was written, so the background loop commits and pushes it. An unknown
subject or topic is 404, an empty title or one that looks like a key 422, a vault that cannot be
opened 503. No session is needed: the book belongs to the topic.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.sessions import SessionService, VaultUnavailableError
from studentassistant.vault import (
    SecretRefused,
    SourceError,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    get_book,
    set_book,
)

SubjectId = Annotated[str, PathParam(pattern=ID_PATTERN)]
TopicId = Annotated[str, PathParam(pattern=ID_PATTERN)]

MAX_TITLE_LENGTH = 200
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
EMPTY_TITLE_DETAIL = "El título del libro no puede estar vacío."
SECRET_DETAIL = "El título parece contener una clave o un token y no se ha guardado."
UNREADABLE_DETAIL = "El libro guardado del tema no se puede leer."


class BookRequest(BaseModel):
    """`PUT` body: the textbook's title as the student gives it."""

    title: str = Field(max_length=MAX_TITLE_LENGTH)


class BookResponse(BaseModel):
    """The topic's textbook; `title` is `null` when none was set."""

    subject_id: str
    topic_id: str
    title: str | None


async def _vault(request: Request) -> Vault:
    service: SessionService = request.app.state.sessions
    try:
        return await service.open_vault()
    except VaultUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
        ) from error


def book_router() -> APIRouter:
    """The topic book routes; the session service is read from `app.state`."""
    router = APIRouter(prefix="/api")

    @router.get(
        "/subjects/{subject_id}/topics/{topic_id}/book",
        responses={404: {"description": "Unknown subject or topic."}},
    )
    async def read_book(request: Request, subject_id: SubjectId, topic_id: TopicId) -> BookResponse:
        vault = await _vault(request)
        try:
            book = await asyncio.to_thread(get_book, vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        except SourceError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNREADABLE_DETAIL) from error
        return BookResponse(
            subject_id=subject_id, topic_id=topic_id, title=None if book is None else book.title
        )

    @router.put(
        "/subjects/{subject_id}/topics/{topic_id}/book",
        responses={
            404: {"description": "Unknown subject or topic."},
            422: {"description": "An empty title, or one that looks like a key."},
        },
    )
    async def write_book(
        request: Request, subject_id: SubjectId, topic_id: TopicId, body: BookRequest
    ) -> BookResponse:
        vault = await _vault(request)
        try:
            book = await asyncio.to_thread(set_book, vault, subject_id, topic_id, body.title)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN_TOPIC_DETAIL) from error
        except SecretRefused as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SECRET_DETAIL) from error
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_TITLE_DETAIL
            ) from error
        except SourceError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNREADABLE_DETAIL) from error
        request.app.state.sessions.note_change()
        return BookResponse(subject_id=subject_id, topic_id=topic_id, title=book.title)

    return router


__all__ = ["BookRequest", "BookResponse", "book_router"]
