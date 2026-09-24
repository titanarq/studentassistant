"""Prices from `[llm.prices]` and `estimate_usd`: known model, cache tokens, unknown model."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from studentassistant.config import DEFAULT_LLM_PRICES, LlmPrice, Settings
from studentassistant.llm import Usage, estimate_usd

PRICES = {
    "claude-test": LlmPrice(
        input_per_mtok=3.0, output_per_mtok=15.0, cache_write_per_mtok=3.75, cache_read_per_mtok=0.3
    )
}


def test_known_model_is_priced_per_million_tokens() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000)

    assert estimate_usd(usage, "claude-test", PRICES) == pytest.approx(3.0 + 1.5)


def test_cache_writes_and_reads_have_their_own_prices() -> None:
    usage = Usage(
        input_tokens=2_000,
        output_tokens=500,
        cache_creation_input_tokens=10_000,
        cache_read_input_tokens=40_000,
    )

    expected = (2_000 * 3.0 + 500 * 15.0 + 10_000 * 3.75 + 40_000 * 0.3) / 1_000_000
    assert estimate_usd(usage, "claude-test", PRICES) == pytest.approx(expected)


def test_no_tokens_cost_nothing() -> None:
    assert estimate_usd(Usage(), "claude-test", PRICES) == 0.0


def test_unknown_model_is_none_and_warned_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="studentassistant.llm.cost"):
        first = estimate_usd(Usage(input_tokens=10), "claude-sin-precio", PRICES)
        second = estimate_usd(Usage(input_tokens=10), "claude-sin-precio", PRICES)

    assert first is None and second is None
    warnings = [r for r in caplog.records if "claude-sin-precio" in r.getMessage()]
    assert len(warnings) == 1


def test_defaults_price_both_default_models_and_set_no_cap(settings: Settings) -> None:
    llm = settings.llm

    assert set(llm.prices) == {llm.roles.observer.model, llm.roles.editor.model}
    assert set(llm.prices) == set(DEFAULT_LLM_PRICES)
    assert llm.max_usd_per_session is None
    assert llm.max_usd_per_day is None


def test_toml_adds_a_model_and_overrides_one_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        textwrap.dedent(
            """
            [llm]
            max_usd_per_session = 1.5
            max_usd_per_day = 5

            [llm.prices."claude-sonnet-5"]
            input_per_mtok = 9.0

            [llm.prices."claude-nuevo"]
            input_per_mtok = 1.0
            output_per_mtok = 2.0
            cache_write_per_mtok = 1.25
            cache_read_per_mtok = 0.1
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SA_CONFIG", str(config))

    llm = Settings().llm

    assert llm.max_usd_per_session == 1.5
    assert llm.max_usd_per_day == 5.0
    sonnet = llm.prices["claude-sonnet-5"]
    assert sonnet.input_per_mtok == 9.0
    assert sonnet.output_per_mtok == DEFAULT_LLM_PRICES["claude-sonnet-5"]["output_per_mtok"]
    assert llm.prices["claude-nuevo"].output_per_mtok == 2.0
    assert "claude-opus-5-5" in llm.prices


def test_env_vars_override_caps_and_prices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_LLM__MAX_USD_PER_DAY", "2.5")
    monkeypatch.setenv("SA_LLM__PRICES__claude-opus-5-5__OUTPUT_PER_MTOK", "30")

    llm = Settings().llm

    assert llm.max_usd_per_day == 2.5
    assert llm.prices["claude-opus-5-5"].output_per_mtok == 30.0
    assert llm.prices["claude-opus-5-5"].input_per_mtok == 4.0
