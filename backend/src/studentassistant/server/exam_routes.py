"""Correcting a topic's mock exam from the web: read it, record a correction, read the results.

Thin: the work is `studentassistant.generators.exam_results`; generating the exam is the generic
`POST .../generated/examen` of `generators_routes`. The vault is opened through the
`SessionService` like the other topic routes.

- `GET .../topics/{topic_id}/exam` -> `StoredExam` (`examen.yaml`: the questions with their
  points, solutions and rubrics; the manifest's `built_at`, notes version and staleness); 404 when
  there is no exam to correct yet.
- `POST .../topics/{topic_id}/exam/results`, body `ExamAttempt` -> `ExamResult`: the points per
  criterion checked, the total and percentage computed, appended to `study/exam-results.jsonl`
  and committed. 404 no exam, 409 the exam was generated again since (`built_at` differs), 422 an
  unknown question, one corrected twice or points out of range.
- `GET .../topics/{topic_id}/exam/results` -> `[ExamResult]`, oldest first.

Errors as `{"detail": "..."}` in Spanish; an unknown topic is 404, a vault that cannot be opened
503.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from studentassistant.generators import GenerationError
from studentassistant.generators.exam_results import (
    ExamAttempt,
    ExamChangedError,
    ExamNotFoundError,
    ExamResult,
    InvalidCorrectionError,
    StoredExam,
    exam_results,
    read_exam,
    record_exam_result,
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
UNREADABLE_DETAIL = "No se puede leer el examen de este tema."

BASE = "/api/subjects/{subject_id}/topics/{topic_id}/exam"


def exam_router() -> APIRouter:
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
    async def exam(request: Request, subject_id: SubjectId, topic_id: TopicId) -> StoredExam:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        try:
            stored = await asyncio.to_thread(read_exam, vault, subject_id, topic_id)
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error
        if stored is None:
            raise HTTPException(status_code=404, detail=str(ExamNotFoundError()))
        return stored

    @router.post(BASE + "/results")
    async def record(
        request: Request, subject_id: SubjectId, topic_id: TopicId, attempt: ExamAttempt
    ) -> ExamResult:
        vault, sync = await open_topic(request, subject_id, topic_id)
        try:
            return await asyncio.to_thread(
                record_exam_result, vault, subject_id, topic_id, attempt, sync=sync
            )
        except ExamNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ExamChangedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InvalidCorrectionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenerationError as error:
            raise HTTPException(status_code=500, detail=UNREADABLE_DETAIL) from error

    @router.get(BASE + "/results")
    async def results(
        request: Request, subject_id: SubjectId, topic_id: TopicId
    ) -> list[ExamResult]:
        vault, _sync = await open_topic(request, subject_id, topic_id)
        return await asyncio.to_thread(exam_results, vault, subject_id, topic_id)

    return router
