"""Scripted Claude answers for the web search tests."""

from __future__ import annotations

from typing import Any

from studentassistant.llm import FakeClaude, LLMResponse, Usage, web_fetch_blocks, web_search_blocks
from studentassistant.sources.web import OFFER_TOOL

HITS = [
    ("https://es.wikipedia.org/wiki/Toma_de_la_Bastilla", "Toma de la Bastilla - Wikipedia"),
    ("https://historia.example.edu/bastilla", "La Bastilla, 14 de julio de 1789"),
]

PAGE_TEXT = "La toma de la Bastilla ocurrió el 14 de julio de 1789 en París."


def offered(*items: tuple[str, str, bool]) -> dict[str, Any]:
    return {
        "results": [
            {"url": url, "title": title, "summary": f"Resumen de {title}.", "relevant": relevant}
            for url, title, relevant in items
        ]
    }


def search_reply(
    fake: FakeClaude,
    results: dict[str, Any] | None = None,
    *,
    query: str = "toma de la Bastilla",
    hits: list[tuple[str, str]] | None = None,
) -> FakeClaude:
    """One answer: a web search with `hits`, then the `offer_results` call."""
    hits = HITS if hits is None else hits
    if results is None:
        results = offered((HITS[0][0], HITS[0][1], True), (HITS[1][0], HITS[1][1], False))
    content = [
        *web_search_blocks(query, hits),
        {"type": "text", "text": "Estas son las páginas."},
        {"type": "tool_use", "id": "toolu_offer", "name": OFFER_TOOL, "input": results},
    ]
    return fake.reply(
        LLMResponse(
            model="",
            stop_reason="tool_use",
            content=content,
            usage=Usage(input_tokens=100, output_tokens=20, web_search_requests=1),
        )
    )


def fetch_reply(
    fake: FakeClaude, url: str = HITS[0][0], text: str = PAGE_TEXT, **kwargs: Any
) -> FakeClaude:
    content = [*web_fetch_blocks(url, text, **kwargs), {"type": "text", "text": "hecho"}]
    return fake.reply(
        LLMResponse(
            model="",
            stop_reason="end_turn",
            content=content,
            usage=Usage(input_tokens=500, output_tokens=2, web_fetch_requests=1),
        )
    )
