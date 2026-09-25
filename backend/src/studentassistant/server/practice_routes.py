"""Practising a topic with spaced repetition from the web (#81).

Thin: the work is `studentassistant.generators.practice`. The vault is opened through the
`SessionService` like the other topic routes.

- `GET .../topics/{topic_id}/practice[?new_limit=n]` -> `PracticeQueue`: the items due now
  (oldest first), then up to `new_limit` (0-100, default 10 a day) never-seen items, with the
  counts, the next due time and Spanish warnings (no material, stale material).
- `POST .../topics/{topic_id}/practice/reviews`, body `PracticeAnswer` -> `ReviewOutcome`: the
  review appended to `study/practice.jsonl` and committed, and the item's new schedule. 404 an item
  no longer in the material, 422 a flashcard review without a rating.
- `POST .../topics/{topic_id}/practice/items/{key}/suspend` and `.../restore` -> `SuspensionOutcome`
  (#281): set an item aside so it is no longer queued, or bring it back with its history. The
  record goes to `study/practice.jsonl` and is committed with the sitting's batch
  (`sync.note_change()`); a repeated request writes nothing (`changed: false`). 404 an item no
  longer in the material. The items set aside are listed in the queue response (`suspended`).

Errors as `{"detail": "..."}` in Spanish; an unknown topic is 404, a vault that cannot be opened
503, a material that cannot be read 500.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request

from studentassistant.generators import GenerationError
from studentassistant.generators.practice import (
    DEFAULT_NEW_LIMIT,
    MAX_NEW_LIMIT,
    InvalidReviewError,
    PracticeAnswer,
    PracticeItemNotFoundError,
    PracticeQueue,
    ReviewOutcome,
    SuspensionOutcome,
    practice_queue,
    record_practice_review,
    restore_practice_item,
    suspend_practice_item,
)
from studentassistant.protocol.base import ID_PATTERN
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
ItemKey = Annotated[str, Path(pattern=r"^(flashcards|quiz):[A-Za-z0-9_-]{1,100}$")]

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
UNREADABLE_DETAIL = "No se puede leer el material de práctica de este tema."

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/practice"


def practice_router() -> APIRouter:
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
    async def queue(
        request: Request,
        subject_id: SubjectId,
        topic_id: TopicId,
        new_limit: Annotated[int, Query(ge=0, le=MAX_NEW_LIMIT)] = DEFAULT_NEW_LIMIT,
    ) -> PracticeQueue:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                practice_queue, vault, subject_id, topic_id, new_limit=new_limit
            )
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error

    @router.post(BASE + "/reviews")
    async def review(
        request: Request, subject_id: SubjectId, topic_id: TopicId, answer: PracticeAnswer
    ) -> ReviewOutcome:
        vault, sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                record_practice_review, vault, subject_id, topic_id, answer, sync=sync
            )
        except PracticeItemNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidReviewError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error

    async def set_aside(
        request: Request, subject_id: str, topic_id: str, key: str, suspend: bool
    ) -> SuspensionOutcome:
        vault, sync = await open_topic(request, subject_id, topic_id)
        action = suspend_practice_item if suspend else restore_practice_item
        try:
            return await asyncio.to_thread(action, vault, subject_id, topic_id, key, sync=sync)
        except PracticeItemNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error

    @router.post(BASE + "/items/{key}/suspend")
    async def suspend(
        request: Request, subject_id: SubjectId, topic_id: TopicId, key: ItemKey
    ) -> SuspensionOutcome:
        return await set_aside(request, subject_id, topic_id, key, True)

    @router.post(BASE + "/items/{key}/restore")
    async def restore(
        request: Request, subject_id: SubjectId, topic_id: TopicId, key: ItemKey
    ) -> SuspensionOutcome:
        return await set_aside(request, subject_id, topic_id, key, False)

    return router
