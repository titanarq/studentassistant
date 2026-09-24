"""One real, minimal Claude call. Marked `integration`: it spends money and needs the network and
an API key, so `scripts/test.sh` never runs it (`uv run pytest -m integration` does)."""

from __future__ import annotations

import asyncio

import pytest

from studentassistant.llm import get_client


@pytest.mark.integration
def test_a_minimal_real_call_answers() -> None:
    client = get_client("observer")

    response = asyncio.run(
        client.create(
            [{"role": "user", "content": "Responde solo con la palabra: hola"}],
            max_tokens=2000,
        )
    )

    assert response.text.strip()
    assert response.usage.output_tokens > 0
