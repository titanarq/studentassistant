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

from studentassistant.server.sessions import SessionService, VaultUnavailableError
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
        sessions: SessionService = request.app.state.sessions
        try:
            vault = await sessions.open_vault()
        except VaultUnavailableError as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, VAULT_UNAVAILABLE_DETAIL
            ) from error
        try:
            items = await asyncio.to_thread(list_feedback, vault, status_filter)
        except JsonlError as error:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, UNREADABLE_DETAIL) from error
        return FeedbackList(items=items)

    return router


__all__ = ["FeedbackList", "feedback_router"]
