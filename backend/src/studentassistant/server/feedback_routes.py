"""`GET /api/feedback`: the vault's app feedback inbox, over REST (#472).

The items the chat recorded (`editor.feedback`, `vault.feedback`): bugs and improvements of the app
the student reported in the workspace chat or the study chat. Read only; the maintainer triages
them with `studentassistant feedback mark`. Authenticated like every `/api` route (the bearer
middleware); the backend never talks to GitHub.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from studentassistant.server.user_scope import active_user_vault
from studentassistant.vault import FeedbackItem, FeedbackStatus, JsonlError, list_feedback

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNREADABLE_DETAIL = "No se puede leer el buzón de comentarios de la bóveda."


class FeedbackList(BaseModel):
    """`GET /api/feedback`: the items, oldest first."""

    items: list[FeedbackItem]


def feedback_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/feedback")
    async def feedback_inbox(
        request: Request,
        status_filter: Annotated[FeedbackStatus | None, Query(alias="status")] = None,
    ) -> FeedbackList:
        vault, _ = await active_user_vault(request)
        try:
            items = await asyncio.to_thread(list_feedback, vault, status_filter)
        except JsonlError as error:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, UNREADABLE_DETAIL) from error
        # The inbox is the repository's; a student sees the reports they made and the ones that
        # name nobody (written before the vault had users), never another student's.
        own = [item for item in items if item.context.user in (None, vault.user_id)]
        return FeedbackList(items=own)

    return router


__all__ = ["FeedbackList", "feedback_router"]
