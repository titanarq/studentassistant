"""`WebSearcher` on a real `SessionBus` and `tmp_vault`, with `FakeClaude` as the searcher.

Every wait is bounded (`asyncio.wait_for`), so a job that never ends fails instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from studentassistant.config import LlmSettings, Settings, SourcesSettings
from studentassistant.llm import FakeClaude
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources.web import (
    WEB_SEARCH_FAILED_KIND,
    WEB_SEARCH_RESULTS_KIND,
    WEB_SNAPSHOT_STORED_KIND,
    list_web_searches,
    record_queued,
)
from studentassistant.sources.web_searcher import (
    VOICE_COMMAND_KIND,
    KeepError,
    NotAWebPageError,
    SearchNotDoneError,
    UnknownSearchError,
    WebSearcher,
    default_client_factory,
)
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    read_ledger,
    start_session,
)
from web_search_helpers import HITS, PAGE_TEXT, fetch_reply, search_reply

pytestmark = pytest.mark.anyio

WAIT = 10.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault) -> Session:
    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "La Revolución francesa").slug
    return start_session(tmp_vault, subject, topic, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _searcher(
    bus: SessionBus, fake: FakeClaude, sources: SourcesSettings | None = None, **settings: Any
) -> WebSearcher:
    sources = sources or SourcesSettings()
    return WebSearcher(
        bus,
        bus.attached,
        settings=sources,
        client_factory=default_client_factory(Settings(sources=sources, **settings), fake),
    )


@pytest.fixture
async def searcher(bus: SessionBus, fake: FakeClaude) -> AsyncIterator[WebSearcher]:
    worker = _searcher(bus, fake)
    worker.start()
    yield worker
    await asyncio.wait_for(worker.stop(), WAIT)


async def _say(bus: SessionBus, session: Session, query: str | None, **extra: Any) -> None:
    payload = {"command": "web_search", "segment_id": "seg-1", "text": f"busca en internet {query}"}
    if query is not None:
        payload["query"] = query
    payload.update(extra)
    await bus.publish(session.id, VOICE_COMMAND_KIND, "stt", payload)


def _kinds(session: Session, kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in session.read_events() if event.kind == kind]


def _where(session: Session) -> tuple[Vault, str, str]:
    return session.vault, session.subject_slug, session.topic_slug


async def test_a_voice_command_runs_a_search_in_the_background(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    search_reply(fake)
    await _say(bus, session, "toma de la Bastilla")
    await asyncio.wait_for(searcher.wait_idle(), WAIT)

    (search,) = list_web_searches(*_where(session))
    assert search.status == "done" and search.requested_by == "voice"
    assert search.session_id == session.id and search.query == "toma de la Bastilla"
    (event,) = _kinds(session, WEB_SEARCH_RESULTS_KIND)
    assert event["search_id"] == search.search_id
    assert [r["url"] for r in event["results"]] == [HITS[0][0], HITS[1][0]]
    # The search asked with the topic's names and was recorded on the session's ledger.
    assert "Tema: La Revolución francesa" in fake.requests[0].messages[0]["content"]
    (entry,) = read_ledger(*_where(session))
    assert entry.session == session.id and entry.role == "observer"
    assert entry.estimated_usd == pytest.approx((100 * 2 + 20 * 10) / 1e6 + 0.01)


async def test_other_commands_and_empty_queries_are_ignored(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    await _say(bus, session, None)
    await _say(bus, session, "   ")
    await bus.publish(session.id, VOICE_COMMAND_KIND, "stt", {"command": "capture"})
    await asyncio.wait_for(searcher.wait_idle(), WAIT)
    assert list_web_searches(*_where(session)) == [] and fake.requests == []


async def test_keep_stores_the_page_and_tells_the_session(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    search_reply(fake)
    search_id = await searcher.submit(*_where(session), "bastilla", session_id=session.id)
    await asyncio.wait_for(searcher.wait_idle(), WAIT)
    fetch_reply(fake, title="Toma de la Bastilla")
    kept = await asyncio.wait_for(searcher.keep(*_where(session), search_id, 0), WAIT)
    assert kept.source_id == "sources/web/001-toma-de-la-bastilla.md"
    assert PAGE_TEXT in kept.path.read_text()
    (event,) = _kinds(session, WEB_SNAPSHOT_STORED_KIND)
    assert event == {
        "search_id": search_id,
        "index": 0,
        "url": HITS[0][0],
        "source_id": kept.source_id,
        "title": "Toma de la Bastilla",
        "kept_by": "student",
    }
    # Keeping it again answers the first snapshot without fetching anything.
    again = await searcher.keep(*_where(session), search_id, 0)
    assert again.source_id == kept.source_id and again.path == kept.path
    assert len(fake.requests) == 2 and len(list_sources(*_where(session))) == 1
    (search,) = await searcher.list(*_where(session))
    assert [(k.index, k.source_id, k.kept_by) for k in search.kept] == [
        (0, kept.source_id, "student")
    ]


async def test_keep_refusals(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    with pytest.raises(UnknownSearchError):
        await searcher.keep(*_where(session), "ws-nada", 0)
    record_queued(*_where(session), search_id="ws-pendiente", query="x", requested_by="web")
    with pytest.raises(SearchNotDoneError):
        await searcher.keep(*_where(session), "ws-pendiente", 0)
    search_reply(fake)
    search_id = await searcher.submit(*_where(session), "bastilla")
    await asyncio.wait_for(searcher.wait_idle(), WAIT)
    with pytest.raises(UnknownSearchError):
        await searcher.keep(*_where(session), search_id, 5)
    fetch_reply(fake, HITS[1][0], media_type="application/pdf", data="JVBERi0=")
    with pytest.raises(KeepError, match="PDF"):
        await searcher.keep(*_where(session), search_id, 1)
    assert list_sources(*_where(session)) == []


async def test_keep_url_stores_a_page_given_by_its_address(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    url = "https://historia.example.edu/bastilla"
    fetch_reply(fake, url, title="La Bastilla")
    kept, already = await asyncio.wait_for(
        searcher.keep_url(*_where(session), f"  {url} ", added_via="share", session_id=session.id),
        WAIT,
    )
    assert not already and kept.source_id == "sources/web/001-la-bastilla.md"
    text = kept.path.read_text()
    assert text.startswith("# La Bastilla\n\n> Copia de <https://historia.example.edu/bastilla>")
    assert PAGE_TEXT in text
    (source,) = list_sources(*_where(session))
    assert source.meta is not None
    assert source.meta["url"] == url and source.meta["external"] is True
    assert source.meta["kept_by"] == "student" and source.meta["added_via"] == "share"
    assert source.meta["session"] == session.id and "search_id" not in source.meta
    (event,) = _kinds(session, WEB_SNAPSHOT_STORED_KIND)
    assert event == {
        "url": url,
        "source_id": kept.source_id,
        "title": "La Bastilla",
        "kept_by": "student",
        "added_via": "share",
    }
    (entry,) = read_ledger(*_where(session))
    assert entry.session == session.id
    # The same address again: the stored snapshot, nothing fetched, nothing published.
    again, already = await searcher.keep_url(*_where(session), url)
    assert already and again.source_id == kept.source_id and again.title == "La Bastilla"
    assert len(fake.requests) == 1 and len(_kinds(session, WEB_SNAPSHOT_STORED_KIND)) == 1


async def test_keep_url_without_a_session_and_its_refusals(
    session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    with pytest.raises(NotAWebPageError):
        await searcher.keep_url(*_where(session), "ftp://example.org/x")
    with pytest.raises(NotAWebPageError):
        await searcher.keep_url(*_where(session), "no es una dirección")
    fetch_reply(fake, HITS[1][0], media_type="application/pdf", data="JVBERi0=")
    with pytest.raises(KeepError, match="PDF"):
        await searcher.keep_url(*_where(session), HITS[1][0])
    fetch_reply(fake, HITS[1][0], title="Bastilla")
    kept, already = await asyncio.wait_for(searcher.keep_url(*_where(session), HITS[1][0]), WAIT)
    assert not already
    (source,) = list_sources(*_where(session))
    assert source.meta is not None and source.meta["added_via"] == "url"
    assert "session" not in source.meta
    assert _kinds(session, WEB_SNAPSHOT_STORED_KIND) == []
    assert kept.url == HITS[1][0]


async def test_a_reached_cost_cap_fails_the_search(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    worker = _searcher(bus, fake, llm=LlmSettings(max_usd_per_day=0))
    worker.start()
    try:
        await _say(bus, session, "bastilla")
        await asyncio.wait_for(worker.wait_idle(), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)
    (search,) = list_web_searches(*_where(session))
    assert (search.status, search.reason) == ("failed", "cost_cap")
    (event,) = _kinds(session, WEB_SEARCH_FAILED_KIND)
    assert event["reason"] == "cost_cap" and fake.requests == []


async def test_auto_keep_keeps_the_relevant_results(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    worker = _searcher(bus, fake, SourcesSettings(web_auto_keep=True))
    worker.start()
    try:
        search_reply(fake)
        fetch_reply(fake, title="Toma de la Bastilla")
        await worker.submit(*_where(session), "bastilla", session_id=session.id)
        await asyncio.wait_for(worker.wait_idle(), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)
    (search,) = list_web_searches(*_where(session))
    assert [(k.index, k.kept_by) for k in search.kept] == [(0, "assistant")]
    assert fake.pending == 0


async def test_a_search_no_job_runs_lists_as_interrupted(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    record_queued(*_where(session), search_id="ws-colgada", query="x", requested_by="web")
    (search,) = await searcher.list(*_where(session))
    assert (search.status, search.reason) == ("failed", "interrupted")


async def test_results_of_an_ended_session_are_only_recorded(
    bus: SessionBus, session: Session, fake: FakeClaude, searcher: WebSearcher
) -> None:
    search_reply(fake)
    search_id = await searcher.submit(*_where(session), "bastilla", session_id="20200101-000000")
    await asyncio.wait_for(searcher.wait_idle(), WAIT)
    (search,) = list_web_searches(*_where(session))
    assert search.search_id == search_id and search.status == "done"
    assert _kinds(session, WEB_SEARCH_RESULTS_KIND) == []
