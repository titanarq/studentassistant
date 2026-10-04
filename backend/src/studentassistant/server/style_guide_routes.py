"""The subject style guide API: read it, confirm the editor's proposed rules, edit the list.

Thin: the work is `studentassistant.editor.style_guide`; web-only, not phone protocol.

- `GET /api/subjects/{s}/style-guide` -> `StyleGuide` (`subject`, `rules`).
- `POST /api/subjects/{s}/style-guide/rules`, body `{"rules": [...]}` -> `StyleGuide` with
  `added` and `commit`: the student confirms rules the editor proposed while revising
  (`RevisionResult.proposed_style_rules`, `ChatTurn.proposed_style_rules`); one already there is
  skipped.
- `PUT /api/subjects/{s}/style-guide`, body `{"rules": [...]}` -> `StyleGuide` with `commit`: the
  whole list, edited, reordered or with rules removed (`[]` clears it).

Errors, Spanish `detail`: a vault that cannot be opened 503, an unknown subject 404, an empty or
too long rule or too many rules 422.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from studentassistant.editor.style_guide import (
    MAX_RULES,
    InvalidStyleRuleError,
    StyleGuide,
    add_style_rules,
    read_style_guide,
    replace_style_rules,
)
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.server.user_scope import active_user_vault
from studentassistant.vault import GitSync, SubjectNotFoundError, Vault

SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]

VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."
UNKNOWN_SUBJECT_DETAIL = "No existe esa asignatura en la bóveda."


class StyleRulesRequest(BaseModel):
    """The rules to add (`POST .../rules`) or the whole list (`PUT`)."""

    rules: list[str] = Field(max_length=MAX_RULES)


def style_guide_router() -> APIRouter:
    router = APIRouter()

    async def user_scope(request: Request) -> tuple[Vault, GitSync]:
        return await active_user_vault(request)

    async def run(function: Callable[..., StyleGuide], *args: Any, **kwargs: Any) -> StyleGuide:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except SubjectNotFoundError as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_SUBJECT_DETAIL) from error
        except InvalidStyleRuleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/api/subjects/{subject_id}/style-guide")
    async def get_guide(request: Request, subject_id: SubjectId) -> StyleGuide:
        vault, _ = await user_scope(request)
        return await run(read_style_guide, vault, subject_id)

    @router.post("/api/subjects/{subject_id}/style-guide/rules")
    async def add_rules(
        request: Request, subject_id: SubjectId, body: StyleRulesRequest
    ) -> StyleGuide:
        vault, sync = await user_scope(request)
        return await run(add_style_rules, vault, subject_id, body.rules, sync=sync)

    @router.put("/api/subjects/{subject_id}/style-guide")
    async def replace_rules(
        request: Request, subject_id: SubjectId, body: StyleRulesRequest
    ) -> StyleGuide:
        vault, sync = await user_scope(request)
        return await run(replace_style_rules, vault, subject_id, body.rules, sync=sync)

    return router
