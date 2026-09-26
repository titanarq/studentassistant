"""Cost of Claude calls: prices from `[llm.prices]`, the per-topic ledger and the cost caps.

A call made by a client bound to a `LedgerBinding` is checked against the caps before it is sent
and, once it succeeds, appended to its topic's `ledger.jsonl` through `studentassistant.vault`
(ADR-0002). Prices and caps come from `LlmSettings` only; a model with no price is recorded with
`estimated_usd = None` (warned about once per model) and adds nothing to the capped totals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from studentassistant.config import LlmPrice, LlmSettings, Settings
from studentassistant.llm.errors import (
    CostCapReachedError,
    CostConfirmationRequiredError,
)
from studentassistant.llm.types import LLMRequest, LLMResponse, Usage
from studentassistant.vault import (
    LedgerEntry,
    Vault,
    append_ledger_entry,
    read_all_ledgers,
    read_ledger,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
CapName = Literal["session", "day"]

# Roles whose calls simply pause at a cap; every other role asks the student to confirm.
NON_ESSENTIAL_ROLES = frozenset({"observer", "transcriber"})

_TOKENS_PER_MTOK = 1_000_000
_unpriced_models_warned: set[str] = set()


def utc_now() -> datetime:
    return datetime.now(UTC)


def estimate_usd(
    usage: Usage,
    model: str,
    prices: Mapping[str, LlmPrice],
    *,
    web_search_usd_per_thousand: float = 0.0,
) -> float | None:
    """USD of one call's `usage` on `model`; `None` (warned once per model) when it has no price.

    `input_tokens` are the uncached ones: cache writes and reads are reported and priced apart.
    Each server-side web search adds `web_search_usd_per_thousand / 1000` (`[llm]`).
    """
    price = prices.get(model)
    if price is None:
        if model not in _unpriced_models_warned:
            _unpriced_models_warned.add(model)
            logger.warning(
                "no price configured for model %r in [llm.prices]: its calls are recorded with "
                "an unknown cost and do not count toward the cost caps",
                model,
            )
        return None
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.output_tokens * price.output_per_mtok
        + usage.cache_creation_input_tokens * price.cache_write_per_mtok
        + usage.cache_read_input_tokens * price.cache_read_per_mtok
    ) / _TOKENS_PER_MTOK + usage.web_search_requests * web_search_usd_per_thousand / 1000


@dataclass(frozen=True)
class LedgerBinding:
    """Where a client's calls are recorded: a topic of a vault and, optionally, its session."""

    vault: Vault
    subject: str
    topic: str
    session: str | None = None


class CostStatus(BaseModel):
    """The spend a binding's caps are measured against, for the server's status endpoint."""

    session_usd: float
    day_usd: float
    max_usd_per_session: float | None
    max_usd_per_day: float | None
    # Both are true once either cap is reached: the observer is paused and the editor asks first.
    observer_paused: bool
    editor_needs_confirmation: bool
    # Calls of a model missing from `[llm.prices]` add 0 to the totals above: they are counted
    # here, per scope, so a client can say the caps may be underestimating the spend.
    unpriced_session_calls: int = 0
    unpriced_day_calls: int = 0
    unpriced_models: list[str] = []


def _usd(entries: Iterable[LedgerEntry]) -> float:
    return sum(entry.estimated_usd or 0.0 for entry in entries)


def session_usd(binding: LedgerBinding) -> float:
    """USD of the bound session's entries (0 when the binding names no session)."""
    return _usd(_session_entries(binding))


def _session_entries(binding: LedgerBinding) -> list[LedgerEntry]:
    if binding.session is None:
        return []
    entries = read_ledger(binding.vault, binding.subject, binding.topic)
    return [entry for entry in entries if entry.session == binding.session]


def _day_entries(vault: Vault, now: datetime) -> list[LedgerEntry]:
    day = now.astimezone(UTC).date()
    return [entry for entry in read_all_ledgers(vault) if entry.time.date() == day]


def day_usd(vault: Vault, now: datetime) -> float:
    """USD of every entry in the vault whose `time` falls on `now`'s UTC day."""
    return _usd(_day_entries(vault, now))


def _reached_cap(
    binding: LedgerBinding, settings: LlmSettings, now: datetime
) -> tuple[CapName, float, float] | None:
    """`(cap, limit, total)` of the first reached cap (session before day), or `None`."""
    limit = settings.max_usd_per_session
    if limit is not None and binding.session is not None:
        total = session_usd(binding)
        if total >= limit:
            return "session", limit, total
    limit = settings.max_usd_per_day
    if limit is not None:
        total = day_usd(binding.vault, now)
        if total >= limit:
            return "day", limit, total
    return None


def check_caps(
    role: str,
    binding: LedgerBinding,
    settings: LlmSettings,
    *,
    now: datetime,
    confirm_over_cap: bool = False,
) -> None:
    """Raise if a cap is reached: `CostCapReachedError` for the observer and the transcriber,
    `CostConfirmationRequiredError` for the editor and the generator unless `confirm_over_cap`."""
    reached = _reached_cap(binding, settings, now)
    if reached is None:
        return
    cap, limit, total = reached
    detail = f"the {cap} cost cap of ${limit:.2f} is reached (${total:.4f} spent)"
    if role in NON_ESSENTIAL_ROLES:
        raise CostCapReachedError(
            f"{role} call paused: {detail}", cap=cap, limit_usd=limit, total_usd=total
        )
    if not confirm_over_cap:
        raise CostConfirmationRequiredError(
            f"{role} call needs confirmation: {detail}", cap=cap, limit_usd=limit, total_usd=total
        )


def record_call(
    binding: LedgerBinding,
    request: LLMRequest,
    response: LLMResponse,
    prices: Mapping[str, LlmPrice],
    *,
    now: datetime,
    web_search_usd_per_thousand: float = 0.0,
) -> LedgerEntry:
    """Append the ledger entry of one successful call and return it."""
    model = response.model or request.model
    usage = response.usage
    # A backend that reports the call's cost itself (Claude Code) is recorded as reported.
    usd = response.reported_usd
    if usd is None:
        usd = estimate_usd(
            usage, model, prices, web_search_usd_per_thousand=web_search_usd_per_thousand
        )
    entry = LedgerEntry(
        time=now,
        role=request.role,
        model=model,
        prompt_hash=request.prompt_hash,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_write_tokens=usage.cache_creation_input_tokens,
        estimated_usd=usd,
        billing=None if response.billing == "api" else response.billing,
        subject=binding.subject,
        topic=binding.topic,
        session=binding.session,
    )
    append_ledger_entry(binding.vault, binding.subject, binding.topic, entry)
    return entry


def cost_status(
    binding: LedgerBinding, settings: Settings | None = None, *, now: datetime | None = None
) -> CostStatus:
    """The binding's session and day spend against the configured caps.

    Entries without a known cost (`estimated_usd = None`) add 0 to the totals and are counted in
    `unpriced_session_calls` / `unpriced_day_calls`, their models in `unpriced_models`.
    """
    llm = (settings or Settings()).llm
    now = now or utc_now()
    reached = _reached_cap(binding, llm, now) is not None
    session_entries = _session_entries(binding)
    day_entries = _day_entries(binding.vault, now)
    unpriced_session = [entry for entry in session_entries if entry.estimated_usd is None]
    unpriced_day = [entry for entry in day_entries if entry.estimated_usd is None]
    return CostStatus(
        session_usd=_usd(session_entries),
        day_usd=_usd(day_entries),
        max_usd_per_session=llm.max_usd_per_session,
        max_usd_per_day=llm.max_usd_per_day,
        observer_paused=reached,
        editor_needs_confirmation=reached,
        unpriced_session_calls=len(unpriced_session),
        unpriced_day_calls=len(unpriced_day),
        unpriced_models=sorted({entry.model for entry in unpriced_session + unpriced_day}),
    )
