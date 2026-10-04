"""The practice waiting today across every topic, in one call (#280).

Thin: the work is `studentassistant.generators.practice.practice_summary`, run in a worker thread.

- `GET /api/practice/summary[?new_limit=n]` -> `PracticeSummary`: per topic with practice material
  (`subject_id`, `subject_name`, `topic_id`, `topic_title`, `due`, `new`, `next_due`), the most due
  first, plus `totals` and Spanish `warnings` naming the topics that could not be read (skipped).

Errors as `{"detail": "..."}` in Spanish; a vault that cannot be opened is 503.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request

from studentassistant.generators.practice import (
    DEFAULT_NEW_LIMIT,
    MAX_NEW_LIMIT,
    PracticeSummary,
    practice_summary,
)
from studentassistant.server.user_scope import active_user_vault

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."


def practice_summary_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/practice/summary")
    async def summary(
        request: Request,
        new_limit: Annotated[int, Query(ge=0, le=MAX_NEW_LIMIT)] = DEFAULT_NEW_LIMIT,
    ) -> PracticeSummary:
        vault, _ = await active_user_vault(request)
        return await asyncio.to_thread(practice_summary, vault, new_limit=new_limit)

    return router
