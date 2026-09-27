"""Web search sources under the Claude Code backend (#306): `WebSearcher` over a real
`ClaudeCodeTransport` driving the fake `claude` executable (its scripted WebSearch / WebFetch).

No network and no real Claude call; every wait is bounded (`asyncio.wait_for`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from llm.fake_claude_cli import FakeClaudeCli, install_fake_claude
from studentassistant.config import Settings, SourcesSettings
from studentassistant.llm import ClaudeCodeTransport
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources.web import (
    OFFER_TOOL,
    WEB_SEARCH_FAILED_KIND,
    WEB_UNAVAILABLE_MESSAGE,
    list_web_searches,
)
from studentassistant.sources.web_searcher import KeepError, WebSearcher, default_client_factory
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    read_conversation,
    read_ledger,
    start_session,
)
from web_search_helpers import HITS, PAGE_TEXT, offered

pytestmark = pytest.mark.anyio

WAIT = 20.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "La Revolución francesa").slug
    return start_session(tmp_vault, subject, topic, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake(tmp_path: Path) -> FakeClaudeCli:
    return install_fake_claude(tmp_path / "fake-claude")


async def _searcher(
    bus: SessionBus, fake: FakeClaudeCli, **cli: Any
) -> AsyncIterator[tuple[WebSearcher, ClaudeCodeTransport]]:
    sources = SourcesSettings()
    transport = ClaudeCodeTransport(fake.settings(**cli), web_role=sources.web_search_role)
    settings = Settings(sources=sources)
    worker = WebSearcher(
        bus,
        bus.attached,
        settings=sources,
        client_factory=default_client_factory(settings, transport),
    )
    worker.start()
    try:
        yield worker, transport
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)
        await asyncio.wait_for(transport.aclose(), WAIT)


@pytest.fixture
async def searcher(bus: SessionBus, fake: FakeClaudeCli) -> AsyncIterator[WebSearcher]:
    async for worker, _ in _searcher(bus, fake):
        yield worker


def _where(session: Session) -> tuple[Vault, str, str]:
    return session.vault, session.subject_slug, session.topic_slug


def _offer_call() -> str:
    results = offered((HITS[0][0], HITS[0][1], True), (HITS[1][0], HITS[1][1], False))
    return json.dumps({"tool_calls": [{"name": OFFER_TOOL, "input": results}]})


async def test_a_search_keeps_the_pages_the_cli_found(
    session: Session, fake: FakeClaudeCli, searcher: WebSearcher
) -> None:
    fake.web_search("toma de la Bastilla", HITS, _offer_call(), cost=0.02)
    search_id = await searcher.submit(
        *_where(session), "toma de la Bastilla", session_id=session.id
    )
    await asyncio.wait_for(searcher.wait_idle(), WAIT)

    (search,) = list_web_searches(*_where(session))
    assert search.search_id == search_id and search.status == "done"
    assert [(r.url, r.relevant, r.found_in_search) for r in search.results] == [
        (HITS[0][0], True, True),
        (HITS[1][0], False, True),
    ]
    (process,) = fake.runs
    argv = process["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    (entry,) = read_ledger(*_where(session))
    assert entry.billing == "subscription" and entry.estimated_usd == pytest.approx(0.02)
    (record,) = [
        r for r in read_conversation(*_where(session), "web-search") if r.kind == "search.results"
    ]
    assert record.usage is not None and record.usage["web_search_requests"] == 1

    # Keeping an offered result fetches it with the CLI's WebFetch and stores its text.
    fake.web_fetch(HITS[0][0], PAGE_TEXT)
    kept = await asyncio.wait_for(searcher.keep(*_where(session), search_id, 0), WAIT)
    assert kept.source_id == "sources/web/001-toma-de-la-bastilla-wikipedia.md"
    assert PAGE_TEXT in kept.path.read_text()
    fetch_argv = fake.runs[-1]["argv"]
    assert fetch_argv[fetch_argv.index("--tools") + 1] == "WebFetch"


async def test_a_url_snapshot_keeps_the_fetched_page(
    session: Session, fake: FakeClaudeCli, searcher: WebSearcher
) -> None:
    url = "https://historia.example.edu/bastilla"
    fake.web_fetch(url, PAGE_TEXT, cost=0.01)

    kept, already = await asyncio.wait_for(
        searcher.keep_url(*_where(session), url, session_id=session.id), WAIT
    )

    assert not already and kept.url == url
    text = kept.path.read_text()
    assert f"> Copia de <{url}>" in text and PAGE_TEXT in text
    (source,) = list_sources(*_where(session))
    assert source.meta is not None and source.meta["added_via"] == "url"
    (entry,) = read_ledger(*_where(session))
    assert entry.billing == "subscription" and entry.session == session.id


async def test_a_web_tool_turned_off_fails_with_the_spanish_message(
    bus: SessionBus, session: Session, fake: FakeClaudeCli
) -> None:
    async for worker, _ in _searcher(bus, fake, web_search_tool="", web_fetch_tool=""):
        await worker.submit(*_where(session), "bastilla", session_id=session.id)
        await asyncio.wait_for(worker.wait_idle(), WAIT)
        with pytest.raises(KeepError) as refused:
            await asyncio.wait_for(worker.keep_url(*_where(session), HITS[0][0]), WAIT)
        assert str(refused.value) == WEB_UNAVAILABLE_MESSAGE

    (search,) = list_web_searches(*_where(session))
    assert (search.status, search.reason, search.message) == (
        "failed",
        "error",
        WEB_UNAVAILABLE_MESSAGE,
    )
    (event,) = [e.payload for e in session.read_events() if e.kind == WEB_SEARCH_FAILED_KIND]
    assert event["message"] == WEB_UNAVAILABLE_MESSAGE
    assert fake.runs == [] and list_sources(*_where(session)) == []


async def test_a_cli_without_web_tools_fails_once_without_retrying(
    session: Session, fake: FakeClaudeCli, searcher: WebSearcher
) -> None:
    fake.reply("Hecho.", cli_tools=[]).reply("Hecho.", cli_tools=[])
    await searcher.submit(*_where(session), "bastilla", session_id=session.id)
    await asyncio.wait_for(searcher.wait_idle(), WAIT)

    (search,) = list_web_searches(*_where(session))
    assert (search.status, search.message) == ("failed", WEB_UNAVAILABLE_MESSAGE)
    assert len(fake.turns) == 1


async def test_a_fetch_that_returns_nothing_shows_the_spanish_message(
    session: Session, fake: FakeClaudeCli, searcher: WebSearcher
) -> None:
    fake.reply("No he podido descargarla.")  # no WebFetch call at all

    with pytest.raises(KeepError) as refused:
        await asyncio.wait_for(searcher.keep_url(*_where(session), HITS[0][0]), WAIT)

    assert str(refused.value) == WEB_UNAVAILABLE_MESSAGE
    assert list_sources(*_where(session)) == []
    assert len(fake.turns) == 1
