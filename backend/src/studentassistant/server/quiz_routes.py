"""Taking a topic's quiz from the web: read it, record an attempt, read the results.

Thin: the work is `studentassistant.generators.quiz`; generating the quiz is the generic
`POST .../generated/quiz` of `generators_routes`. The vault is opened through the `SessionService`
like the other topic routes.

- `GET .../topics/{topic_id}/quiz` -> `StoredQuiz` (the questions with their answers and
  explanations, the manifest's `built_at` and notes version); 404 when there is no quiz yet.
- `POST .../topics/{topic_id}/quiz/results`, body `QuizAttempt` -> `QuizResult`: graded, appended
  to `study/quiz-results.jsonl` and committed. 404 no quiz, 409 the quiz was generated again since
  (`built_at` differs), 422 an answer to a question the quiz lacks. A partial attempt
  (`questions`: the ids asked, e.g. the ones answered wrong) grades only those.
- `GET .../topics/{topic_id}/quiz/results` -> `[QuizResult]`, oldest first.

Errors as `{"detail": "..."}` in Spanish; an unknown topic is 404, a vault that cannot be opened
503.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from studentassistant.generators import GenerationError
from studentassistant.generators.quiz import (
    InvalidAttemptError,
    QuizAttempt,
    QuizChangedError,
    QuizNotFoundError,
    QuizResult,
    StoredQuiz,
    quiz_results,
    read_quiz,
    record_quiz_result,
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

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
UNREADABLE_DETAIL = "No se puede leer el quiz de este tema."

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/quiz"


def quiz_router() -> APIRouter:
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
    async def quiz(request: Request, subject_id: SubjectId, topic_id: TopicId) -> StoredQuiz:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        try:
            stored = await asyncio.to_thread(read_quiz, vault, subject_id, topic_id)
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error
        if stored is None:
            raise HTTPException(status_code=404, detail=str(QuizNotFoundError()))
        return stored

    @router.post(BASE + "/results")
    async def record(
        request: Request, subject_id: SubjectId, topic_id: TopicId, attempt: QuizAttempt
    ) -> QuizResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                record_quiz_result, vault, subject_id, topic_id, attempt, sync=sync
            )
        except QuizNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except QuizChangedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InvalidAttemptError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error

    @router.get(BASE + "/results")
    async def results(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> list[QuizResult]:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(quiz_results, vault, subject_id, topic_id)

    return router
