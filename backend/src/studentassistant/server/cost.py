"""`GET /api/cost` and `GET /api/subjects/{s}/topics/{t}/cost`: what Claude calls cost, over REST.

`GET /api/cost` is the cost-cap status of the llm module: the web and the capture clients read it
to show that the observer is paused or that editor calls need confirmation. The server computes
nothing there: it opens the configured vault and returns `studentassistant.llm.cost_status` as is.

`GET /api/subjects/{s}/topics/{t}/cost` is a topic's spend for its page: the topic's ledger entries
(read through `studentassistant.vault.read_ledger`, ADR-0002) summed per session and for the calls
bound to no session (editor, generators). No price is computed here: each entry's `estimated_usd`
was set by the llm module when the call was made, and an entry without one is counted as unpriced.

The configuration is read on every request, so a cap or the vault changed in `config.toml` shows
up without restarting the server.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel

from studentassistant.config import Settings
from studentassistant.llm import CostStatus, LedgerBinding, cost_status
from studentassistant.protocol.base import ID_PATTERN
from studentassistant.vault import (
    LedgerEntry,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    VaultError,
    get_topic,
    list_sessions,
    read_ledger,
)

TOPIC_REQUIRED_DETAIL = "Indica a la vez «subject» y «topic» (y, si quieres, «session»)."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."


SubjectId = Annotated[str, Path(pattern=ID_PATTERN)]
TopicId = Annotated[str, Path(pattern=ID_PATTERN)]


class CostTotals(BaseModel):
    """The summed ledger entries of one scope. `usd` leaves out the `unpriced_calls`."""

    usd: float = 0.0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    unpriced_calls: int = 0


class SessionCost(CostTotals):
    """One session of the topic: its id, its start (epoch ms, `None` if unknown), its spend."""

    session_id: str
    started_at_ms: int | None


class TopicCost(BaseModel):
    """`GET /api/subjects/{s}/topics/{t}/cost`: the topic's total, per session and without one."""

    subject_id: str
    topic_id: str
    total: CostTotals
    sessions: list[SessionCost]
    no_session: CostTotals


def _totals(entries: Iterable[LedgerEntry]) -> CostTotals:
    totals = CostTotals()
    for entry in entries:
        totals.usd += entry.estimated_usd or 0.0
        totals.input_tokens += entry.input_tokens
        totals.output_tokens += entry.output_tokens
        totals.cache_read_tokens += entry.cache_read_tokens
        totals.cache_write_tokens += entry.cache_write_tokens
        totals.calls += 1
        totals.unpriced_calls += entry.estimated_usd is None
    totals.tokens = (
        totals.input_tokens
        + totals.output_tokens
        + totals.cache_read_tokens
        + totals.cache_write_tokens
    )
    return totals


def topic_cost(vault: Vault, subject: str, topic: str) -> TopicCost:
    """The topic's ledger summed: every session the topic lists (spent or not) and every session
    id the ledger names, in id (= start) order, then the calls bound to no session.

    Raises the vault's errors for an unknown or unreadable topic.
    """
    stored = list_sessions(vault, subject, topic)
    entries = read_ledger(vault, subject, topic)
    started = {meta.id: int(meta.started_at.timestamp() * 1000) for meta in stored}
    by_session: dict[str, list[LedgerEntry]] = {meta.id: [] for meta in stored}
    for entry in entries:
        if entry.session is not None:
            by_session.setdefault(entry.session, []).append(entry)
    sessions = []
    for session_id in sorted(by_session):
        session_entries = by_session[session_id]
        start = started.get(session_id)
        if start is None and session_entries:
            # A session the topic no longer lists: its first call is the best start we know.
            start = int(min(entry.time for entry in session_entries).timestamp() * 1000)
        sessions.append(
            SessionCost(
                session_id=session_id,
                started_at_ms=start,
                **_totals(session_entries).model_dump(),
            )
        )
    return TopicCost(
        subject_id=subject,
        topic_id=topic,
        total=_totals(entries),
        sessions=sessions,
        no_session=_totals(entry for entry in entries if entry.session is None),
    )


def _open_vault() -> Vault:
    try:
        return Vault.open(Settings().vault.path)
    except VaultError as error:
        raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error


def cost_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/cost")
    def cost(
        subject: str | None = None, topic: str | None = None, session: str | None = None
    ) -> CostStatus:
        if (subject is None) != (topic is None) or (session is not None and subject is None):
            raise HTTPException(status_code=422, detail=TOPIC_REQUIRED_DETAIL)
        settings = Settings()
        try:
            vault = Vault.open(settings.vault.path)
        except VaultError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        if subject is None or topic is None:
            # No session selected: `session_usd` is 0 and only the day total drives the flags.
            binding = LedgerBinding(vault=vault, subject="", topic="")
        else:
            try:
                get_topic(vault, subject, topic)
            except (SubjectNotFoundError, TopicNotFoundError) as error:
                raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
            binding = LedgerBinding(vault=vault, subject=subject, topic=topic, session=session)
        return cost_status(binding, settings)

    @router.get("/api/subjects/{subject_id}/topics/{topic_id}/cost")
    def cost_of_topic(subject_id: SubjectId, topic_id: TopicId) -> TopicCost:
        vault = _open_vault()
        try:
            return topic_cost(vault, subject_id, topic_id)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error

    return router
