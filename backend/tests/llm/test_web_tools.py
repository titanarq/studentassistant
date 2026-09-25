"""Server-side web tools: definitions, `pause_turn` resumption, result parsing, cost."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studentassistant.config import LlmPrice, LlmSettings
from studentassistant.llm import (
    FakeClaude,
    LedgerBinding,
    LLMResponse,
    Usage,
    estimate_usd,
    parse_web_results,
    run_server_tools,
    web_fetch_blocks,
    web_fetch_tool,
    web_search_blocks,
    web_search_tool,
)
from studentassistant.llm.transport import response_from_message
from studentassistant.vault import Vault, create_subject, create_topic, read_ledger

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_tool_definitions_follow_the_configured_versions() -> None:
    assert web_search_tool() == {"type": "web_search_20260209", "name": "web_search"}
    assert web_fetch_tool() == {"type": "web_fetch_20260209", "name": "web_fetch"}
    settings = LlmSettings(web_search_tool="web_search_20250305", web_fetch_tool="web_fetch_x")
    assert web_search_tool(settings, max_uses=2, allowed_domains=["es.wikipedia.org"]) == {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 2,
        "allowed_domains": ["es.wikipedia.org"],
    }
    assert web_fetch_tool(settings, max_uses=1, max_content_tokens=5000) == {
        "type": "web_fetch_x",
        "name": "web_fetch",
        "max_uses": 1,
        "max_content_tokens": 5000,
    }


def test_parse_hits_documents_and_errors() -> None:
    content = [
        *web_search_blocks("vapor", [("https://a.es/v", "Vapor"), ("https://b.es", "B")]),
        *web_search_blocks("otra", [("https://a.es/v", "Vapor otra vez")], tool_id="s2"),
        *web_search_blocks("rota", [], tool_id="s3", error_code="too_many_requests"),
        *web_fetch_blocks("https://a.es/v", "Texto de la página", title="Vapor"),
        *web_fetch_blocks("https://c.es/doc.pdf", media_type="application/pdf", data="JVBE"),
        *web_fetch_blocks("https://d.es", error_code="url_not_accessible", tool_id="f3"),
        {"type": "text", "text": "hecho"},
    ]
    found = parse_web_results(content)
    assert found.queries == ["vapor", "otra", "rota"]
    assert [(hit.url, hit.title) for hit in found.hits] == [
        ("https://a.es/v", "Vapor"),
        ("https://b.es", "B"),
    ]
    assert found.search_errors == ["too_many_requests"]
    assert found.fetch_errors == ["url_not_accessible"]
    text, pdf = found.documents
    assert (text.url, text.title, text.text, text.media_type) == (
        "https://a.es/v",
        "Vapor",
        "Texto de la página",
        "text/plain",
    )
    assert text.retrieved_at == "2026-09-25T10:00:00Z"
    assert pdf.text is None and pdf.data == "JVBE" and pdf.media_type == "application/pdf"


def _paused() -> LLMResponse:
    return LLMResponse(
        model="",
        stop_reason="pause_turn",
        content=web_search_blocks("vapor", [("https://a.es", "A")]),
    )


async def test_run_server_tools_resumes_a_paused_turn() -> None:
    fake = FakeClaude().reply(_paused()).reply_text("listo")
    client = fake.client("observer")
    messages = [{"role": "user", "content": "busca"}]
    run = await run_server_tools(client, messages, tools=[web_search_tool()])
    assert [r.stop_reason for r in run.responses] == ["pause_turn", "end_turn"]
    assert run.final.text == "listo"
    second = fake.requests[1].messages
    # The paused assistant turn is sent back as it came, with no extra user message after it.
    assert second[0] == messages[0]
    assert second[1]["role"] == "assistant" and second[1]["content"] == _paused().content
    assert len(second) == 2
    assert len(run.content) == len(_paused().content) + 1


async def test_run_server_tools_stops_after_max_continuations() -> None:
    fake = FakeClaude()
    for _ in range(3):
        fake.reply(_paused())
    run = await run_server_tools(
        fake.client("observer"), [{"role": "user", "content": "x"}], max_continuations=2
    )
    assert len(run.responses) == 3 and run.final.stop_reason == "pause_turn"
    assert fake.pending == 0


def test_web_searches_are_priced_per_thousand() -> None:
    prices = {
        "m": LlmPrice(
            input_per_mtok=1, output_per_mtok=1, cache_write_per_mtok=1, cache_read_per_mtok=1
        )
    }
    usage = Usage(input_tokens=1_000_000, web_search_requests=3)
    assert estimate_usd(usage, "m", prices) == pytest.approx(1.0)
    assert estimate_usd(usage, "m", prices, web_search_usd_per_thousand=10) == pytest.approx(1.03)
    assert LlmSettings().web_search_usd_per_thousand == 10.0


async def test_ledger_entries_include_the_web_search_price(tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "Revolución").slug
    fake = FakeClaude().reply_text("x", usage=Usage(web_search_requests=2))
    client = fake.client("observer", ledger=LedgerBinding(tmp_vault, subject, topic))
    await client.create([{"role": "user", "content": "hola"}])
    (entry,) = read_ledger(tmp_vault, subject, topic)
    assert entry.estimated_usd == pytest.approx(0.02)


def test_transport_reads_server_tool_use_counts() -> None:
    message = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        content=[],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
            server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=1),
        ),
    )
    usage = response_from_message(message).usage
    assert (usage.web_search_requests, usage.web_fetch_requests) == (2, 1)
    message.usage.server_tool_use = None
    assert response_from_message(message).usage.web_search_requests == 0
