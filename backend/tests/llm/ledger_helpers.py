"""Shared bits of the ledger/cap tests: a topic, a binding and settings with caps and prices."""

from __future__ import annotations

from datetime import UTC, datetime

from studentassistant.config import LlmPrice, Settings
from studentassistant.llm import LedgerBinding
from studentassistant.vault import LedgerEntry, Vault, create_subject, create_topic

MODEL = "claude-sonnet-5"
# $1 per million of every kind: 1_000_000 input tokens cost exactly $1.
FLAT = LlmPrice(
    input_per_mtok=1.0, output_per_mtok=1.0, cache_write_per_mtok=1.0, cache_read_per_mtok=1.0
)
SESSION = "20260924-100000"
NOON = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def make_topic(vault: Vault, title: str = "Derivadas") -> tuple[str, str]:
    """`(subject slug, topic slug)` of a new topic `title` under a new subject."""
    subject = create_subject(vault, f"Matemáticas {title}").slug
    return subject, create_topic(vault, subject, title).slug


def binding(vault: Vault, topic: tuple[str, str], session: str | None = SESSION) -> LedgerBinding:
    return LedgerBinding(vault=vault, subject=topic[0], topic=topic[1], session=session)


def capped_settings(
    base: Settings, *, per_session: float | None = None, per_day: float | None = None
) -> Settings:
    llm = base.llm.model_copy(
        update={
            "max_usd_per_session": per_session,
            "max_usd_per_day": per_day,
            "prices": {**base.llm.prices, MODEL: FLAT, "claude-opus-5-5": FLAT},
        }
    )
    return base.model_copy(update={"llm": llm})


def seeded_entry(topic: tuple[str, str], usd: float | None, **overrides: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "time": NOON,
        "role": "editor",
        "model": MODEL,
        "input_tokens": 100,
        "output_tokens": 10,
        "estimated_usd": usd,
        "subject": topic[0],
        "topic": topic[1],
        "session": SESSION,
    }
    fields.update(overrides)
    return LedgerEntry.model_validate(fields)
