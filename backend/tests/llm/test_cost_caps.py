"""Cost caps: checked before each bound call from the ledger totals of the session and UTC day."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from ledger_helpers import NOON, binding, capped_settings, make_topic, seeded_entry

from studentassistant.config import Settings
from studentassistant.llm import (
    CostCapReachedError,
    CostConfirmationRequiredError,
    CostStatus,
    FakeClaude,
    LLMError,
    Usage,
    cost_status,
)
from studentassistant.vault import Vault, append_ledger_entry, create_topic, read_ledger

# At the flat $1/MTok of `capped_settings`, this usage costs $0.50.
HALF_A_DOLLAR = Usage(input_tokens=500_000)


def seed(vault: Vault, topic: tuple[str, str], usd: float | None, **overrides: object) -> None:
    append_ledger_entry(vault, *topic, seeded_entry(topic, usd, **overrides))


def call(fake: FakeClaude, role: str, settings: Settings, bound, **kwargs: object):
    client = fake.client(role, settings=settings, ledger=bound, clock=lambda: NOON)
    return asyncio.run(client.create([{"role": "user", "content": "hola"}], **kwargs))


def test_both_errors_are_llm_errors() -> None:
    assert issubclass(CostCapReachedError, LLMError)
    assert issubclass(CostConfirmationRequiredError, LLMError)


def test_session_cap_pauses_the_observer_once_reached(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    capped = capped_settings(settings, per_session=1.0)
    bound = binding(tmp_vault, topic)
    fake = (
        FakeClaude().reply_text("uno", usage=HALF_A_DOLLAR).reply_text("dos", usage=HALF_A_DOLLAR)
    )

    call(fake, "observer", capped, bound)
    call(fake, "observer", capped, bound)  # $0.50 spent: still under the cap
    with pytest.raises(CostCapReachedError) as raised:
        call(fake, "observer", capped, bound)  # $1.00 spent: the cap is reached

    assert (raised.value.cap, raised.value.limit_usd) == ("session", 1.0)
    assert raised.value.total_usd == pytest.approx(1.0)
    assert len(fake.requests) == 2  # the paused call was never sent
    assert len(read_ledger(tmp_vault, *topic)) == 2


def test_session_cap_counts_only_the_bound_session(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 5.0, session="20260923-090000")
    seed(tmp_vault, topic, 5.0, session=None)
    fake = FakeClaude().reply_text("ok")

    call(fake, "transcriber", capped_settings(settings, per_session=1.0), binding(tmp_vault, topic))

    assert len(fake.requests) == 1


def test_day_cap_sums_every_topic_of_the_utc_day(tmp_vault: Vault, settings: Settings) -> None:
    first, second = make_topic(tmp_vault), make_topic(tmp_vault, "Integrales")
    seed(tmp_vault, first, 0.6)
    seed(tmp_vault, second, 0.5, session="20260924-080000")
    capped = capped_settings(settings, per_day=1.0)

    with pytest.raises(CostCapReachedError) as raised:
        call(FakeClaude().reply_text("no"), "observer", capped, binding(tmp_vault, first))

    assert raised.value.cap == "day"
    assert raised.value.total_usd == pytest.approx(1.1)


def test_day_cap_resets_at_utc_midnight(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    # 23:30 at UTC+2 the day before is 21:30 UTC the day before: not today's spend.
    yesterday = datetime(2026, 9, 23, 23, 30, tzinfo=UTC) - timedelta(hours=2)
    seed(tmp_vault, topic, 50.0, time=yesterday)
    seed(tmp_vault, topic, 50.0, time=datetime(2026, 9, 23, 23, 59, 59, tzinfo=UTC))
    fake = FakeClaude().reply_text("ok")

    call(fake, "observer", capped_settings(settings, per_day=1.0), binding(tmp_vault, topic))

    assert len(fake.requests) == 1


def test_unpriced_entries_count_as_zero(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, None, model="claude-desconocido")
    fake = FakeClaude().reply_text("ok")

    call(fake, "observer", capped_settings(settings, per_day=0.01), binding(tmp_vault, topic))

    assert len(fake.requests) == 1


@pytest.mark.parametrize("role", ["editor", "generator"])
def test_user_driven_calls_ask_for_confirmation(
    tmp_vault: Vault, settings: Settings, role: str
) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 2.0)
    fake = FakeClaude().reply_text("ok")

    with pytest.raises(CostConfirmationRequiredError) as raised:
        call(fake, role, capped_settings(settings, per_session=1.5), binding(tmp_vault, topic))

    error = raised.value
    assert (error.cap, error.limit_usd, error.total_usd) == ("session", 1.5, 2.0)
    assert fake.requests == []


def test_confirm_over_cap_proceeds_and_records(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 2.0)
    fake = FakeClaude().reply_text("ok", usage=HALF_A_DOLLAR)
    capped = capped_settings(settings, per_session=1.5, per_day=1.5)

    response = call(fake, "editor", capped, binding(tmp_vault, topic), confirm_over_cap=True)

    assert response.text == "ok"
    assert read_ledger(tmp_vault, *topic)[-1].estimated_usd == pytest.approx(0.5)


def test_confirm_over_cap_does_not_unpause_the_observer(
    tmp_vault: Vault, settings: Settings
) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 2.0)

    with pytest.raises(CostCapReachedError):
        call(
            FakeClaude().reply_text("ok"),
            "observer",
            capped_settings(settings, per_day=1.0),
            binding(tmp_vault, topic),
            confirm_over_cap=True,
        )


def test_no_cap_configured_never_stops_a_call(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 1_000.0)
    fake = FakeClaude().reply_text("a").reply_text("b")

    call(fake, "observer", capped_settings(settings), binding(tmp_vault, topic))
    call(fake, "editor", capped_settings(settings), binding(tmp_vault, topic))

    assert len(fake.requests) == 2


def test_without_a_binding_no_cap_applies(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 1_000.0)
    fake = FakeClaude().reply_text("ok")

    call(fake, "observer", capped_settings(settings, per_session=0.0, per_day=0.0), None)

    assert len(fake.requests) == 1


def test_cost_status_reports_totals_and_flags(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    other = (topic[0], create_topic(tmp_vault, topic[0], "Límites").slug)
    seed(tmp_vault, topic, 0.25)
    seed(tmp_vault, other, 0.5, session="20260924-110000")
    bound = binding(tmp_vault, topic)

    under = cost_status(bound, capped_settings(settings, per_session=1.0, per_day=1.0), now=NOON)
    over = cost_status(bound, capped_settings(settings, per_day=0.75), now=NOON)

    assert under == CostStatus(
        session_usd=0.25,
        day_usd=0.75,
        max_usd_per_session=1.0,
        max_usd_per_day=1.0,
        observer_paused=False,
        editor_needs_confirmation=False,
    )
    assert over.observer_paused and over.editor_needs_confirmation
    assert over.max_usd_per_session is None


def test_cost_status_without_a_session(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 3.0)

    status = cost_status(
        binding(tmp_vault, topic, session=None),
        capped_settings(settings, per_session=1.0),
        now=NOON,
    )

    assert status.session_usd == 0.0
    assert status.day_usd == 3.0
    assert not status.observer_paused


def test_cost_status_counts_unpriced_calls_per_scope(tmp_vault: Vault, settings: Settings) -> None:
    topic = make_topic(tmp_vault)
    seed(tmp_vault, topic, 0.25)
    seed(tmp_vault, topic, None, model="claude-mystery-1")
    seed(tmp_vault, topic, None, model="claude-mystery-2", session="20260924-110000")

    status = cost_status(binding(tmp_vault, topic), capped_settings(settings), now=NOON)

    assert status.session_usd == 0.25
    assert status.day_usd == 0.25
    assert status.unpriced_session_calls == 1
    assert status.unpriced_day_calls == 2
    assert status.unpriced_models == ["claude-mystery-1", "claude-mystery-2"]
