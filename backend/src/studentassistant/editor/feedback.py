"""App feedback from the chat: the student reports a bug or an improvement of the app itself (#472).

In the workspace chat (a revision turn, Construir) and the study chat (the written tutor,
Estudiar) the editor is offered one more strict tool, `report_feedback` (`FeedbackReport`:
`kind` `bug` | `mejora`, a short Spanish `title`, a `body` with the student's words and a
summary). The rule of when to call it is the prompt `editor_feedback`, appended to the turn's
instruction: only for feedback about the app («apunta una mejora: ...», «esto es un bug: ...»,
«la app debería ...»), never for the notes' content, and never together with a change of the
notes. A valid call is stored with `vault.add_feedback` in the vault's `feedback/inbox.jsonl`
(with the turn's context: subject, topic, route, session, mode and a short excerpt of the chat),
and the turn's result carries a `FeedbackRef` the web shows as a chip («Mejora apuntada» /
«Bug apuntado»). The backend never calls GitHub: the maintainer triages the inbox with
`studentassistant feedback list` / `mark`.

A malformed call or a store that fails never fails the turn: nothing is recorded and the result's
`warning` says so (`NOT_RECORDED_WARNING`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from studentassistant.llm import LLMResponse, load_prompt, strict_tool
from studentassistant.vault import (
    FeedbackContext,
    FeedbackKind,
    FeedbackMode,
    GitSync,
    Vault,
    add_feedback,
)
from studentassistant.vault.feedback import (
    MAX_BODY_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_TITLE_CHARS,
    collapse_title,
)

logger = logging.getLogger(__name__)

FEEDBACK_TOOL = "report_feedback"
FEEDBACK_PROMPT_NAME = "editor_feedback"
EXCERPT_CHARS = 600
"""How much of the chat before the report is kept with it: the last lines, kept short."""

NOT_RECORDED_WARNING = "No he podido apuntar tu comentario sobre la aplicación; vuelve a decírmelo."
CHIP_LABELS: dict[FeedbackKind, str] = {"bug": "Bug apuntado", "mejora": "Mejora apuntada"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedbackReport(_Strict):
    """The input of the `report_feedback` tool: one bug or improvement of the app itself."""

    kind: Literal["bug", "mejora"] = Field(
        description="`bug`: something of the app does not work as it should; `mejora`: an"
        " improvement or a new feature the student asks for."
    )
    title: str = Field(
        min_length=1,
        max_length=MAX_TITLE_CHARS,
        description=f"A short Spanish title of the item, at most {MAX_TITLE_CHARS} characters"
        " (e.g. «El botón de capturar no responde en el móvil»).",
    )
    body: str = Field(
        min_length=1,
        description="Spanish: the student's own words, quoted, then one or two sentences"
        " summarising what they report and where it happens.",
    )

    @field_validator("title", mode="before")
    @classmethod
    def one_line(cls, value: object) -> object:
        # the vault's limit counts the collapsed title, so this one does too: a longer title is
        # a malformed call (nothing recorded, the turn's warning says so), never cut silently
        return collapse_title(value) if isinstance(value, str) else value


class FeedbackRef(_Strict):
    """What a chat turn recorded: the inbox item, as the chip shows it."""

    id: str
    kind: Literal["bug", "mejora"]
    title: str


def feedback_tool() -> dict[str, Any]:
    """The strict `report_feedback` tool offered next to a chat turn's own tools."""
    return strict_tool(
        FEEDBACK_TOOL,
        "Record a bug report or an improvement request about the Student Assistant app itself"
        " (not about the notes or the topic) so the developers can plan it.",
        FeedbackReport,
    )


def feedback_instruction() -> str:
    """The rule of when to call the tool, appended to the chat turn's instruction."""
    return "\n" + load_prompt(FEEDBACK_PROMPT_NAME).content.strip() + "\n"


def parse_feedback(response: LLMResponse) -> FeedbackReport | None:
    """The first valid `report_feedback` call of `response`; `None` without one (or malformed)."""
    for call in response.tool_calls:
        if call.name != FEEDBACK_TOOL:
            continue
        try:
            return FeedbackReport.model_validate(json.loads(call.input_json))
        except (json.JSONDecodeError, ValidationError):
            logger.warning("ignoring a malformed %s call", FEEDBACK_TOOL)
            return None
    return None


def has_feedback_call(response: LLMResponse) -> bool:
    return any(call.name == FEEDBACK_TOOL for call in response.tool_calls)


def confirmation(ref: FeedbackRef) -> str:
    """The Spanish reply of a turn that only recorded feedback and wrote nothing itself."""
    what = "el bug" if ref.kind == "bug" else "la mejora"
    return f"He apuntado {what}: {ref.title}."


def excerpt(lines: list[str], message: str) -> str:
    """The last lines of the chat plus the new message, at most `EXCERPT_CHARS` (the newest)."""
    text = "\n".join([*lines, f"Estudiante: {message}"]).strip()
    return text[-EXCERPT_CHARS:] if len(text) > EXCERPT_CHARS else text


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def record_feedback(
    vault: Vault,
    report: FeedbackReport,
    *,
    subject_slug: str,
    topic_slug: str,
    mode: FeedbackMode,
    route: str,
    session_id: str | None,
    chat_excerpt: str,
    sync: GitSync | None,
    clock: Callable[[], datetime] | None = None,
) -> FeedbackRef | None:
    """Store `report` in the vault's inbox; `None` (logged) when it cannot be stored."""
    context = FeedbackContext(
        subject=subject_slug,
        topic=topic_slug,
        route=route,
        session_id=session_id,
        mode=mode,
        excerpt=_cut(chat_excerpt, MAX_EXCERPT_CHARS),
    )
    try:
        item = await asyncio.to_thread(
            add_feedback,
            vault,
            report.kind,
            report.title,
            _cut(report.body, MAX_BODY_CHARS),
            context,
            clock=clock,
        )
    except Exception:
        logger.exception("could not record app feedback from %s/%s", subject_slug, topic_slug)
        return None
    if sync is not None:
        sync.note_change()
    return FeedbackRef(id=item.id, kind=item.kind, title=item.title)


__all__ = [
    "CHIP_LABELS",
    "FEEDBACK_TOOL",
    "NOT_RECORDED_WARNING",
    "FeedbackRef",
    "FeedbackReport",
    "confirmation",
    "excerpt",
    "feedback_instruction",
    "feedback_tool",
    "has_feedback_call",
    "parse_feedback",
    "record_feedback",
]
