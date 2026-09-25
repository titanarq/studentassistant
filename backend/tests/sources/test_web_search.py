"""`sources.web`: searching, fetching, storing a snapshot and folding a topic's search log."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studentassistant.config import SourcesSettings
from studentassistant.llm import FakeClaude, LLMResponse, RefusalError
from studentassistant.sources.web import (
    OFFER_TOOL,
    KeptWebSource,
    WebFetchError,
    WebSearchError,
    keep_snapshot,
    list_web_searches,
    new_search_id,
    record_failed,
    record_kept,
    record_queued,
    record_results,
    search_web,
    snapshot_page,
)
from studentassistant.vault import (
    SecretRefused,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    read_source,
)
from web_search_helpers import HITS, PAGE_TEXT, fetch_reply, offered, search_reply

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Revolución francesa").slug


async def test_search_offers_the_pages_claude_found() -> None:
    fake = search_reply(FakeClaude())
    search = await search_web(
        fake.client("observer"),
        "  toma de la Bastilla ",
        settings=SourcesSettings(web_search_max_uses=2, web_search_max_results=4),
        subject="Historia",
        topic="La Revolución francesa",
    )
    assert search.query == "toma de la Bastilla"
    assert [r.url for r in search.results] == [HITS[0][0], HITS[1][0]]
    assert [r.relevant for r in search.results] == [True, False]
    assert all(r.found_in_search for r in search.results)
    assert search.queries == ["toma de la Bastilla"]
    request = fake.requests[0]
    assert request.tools[0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 2}
    assert request.tools[1]["name"] == OFFER_TOOL and request.tools[1]["strict"] is True
    assert request.tool_choice is None
    text = request.messages[0]["content"]
    assert "Asignatura: Historia" in text and "Tema: La Revolución francesa" in text
    assert "Búsqueda pedida: toma de la Bastilla" in text and "como mucho 4" in text


async def test_search_cleans_what_claude_offers() -> None:
    results = offered(
        ("ftp://raro.example/x", "No web", True),
        (HITS[0][0], "Primera", True),
        (HITS[0][0], "Repetida", True),
        ("https://inventada.example/p", "Inventada", True),
        (HITS[1][0], "Tercera", False),
    )
    fake = search_reply(FakeClaude(), results)
    search = await search_web(
        fake.client("observer"), "bastilla", settings=SourcesSettings(web_search_max_results=2)
    )
    assert [(r.title, r.found_in_search) for r in search.results] == [
        ("Primera", True),
        ("Inventada", False),
    ]


async def test_search_reasks_once_then_gives_up() -> None:
    fake = FakeClaude().reply_text("Aquí tienes: ...")
    search_reply(fake)
    search = await search_web(fake.client("observer"), "bastilla")
    assert len(search.results) == 2 and len(search.responses) == 2
    assert "did not call the `offer_results` tool" in fake.requests[1].messages[-1]["content"]

    fake = FakeClaude().reply_text("nada").reply_text("nada otra vez")
    with pytest.raises(WebSearchError):
        await search_web(fake.client("observer"), "bastilla")


async def test_search_refusal_and_empty_query() -> None:
    fake = FakeClaude().reply(LLMResponse(model="", stop_reason="refusal", content=[]))
    with pytest.raises(RefusalError):
        await search_web(fake.client("observer"), "bastilla")
    with pytest.raises(WebSearchError):
        await search_web(FakeClaude().client("observer"), "   ")


async def test_snapshot_takes_the_fetched_text() -> None:
    fake = fetch_reply(FakeClaude(), title="Toma de la Bastilla")
    snapshot = await snapshot_page(
        fake.client("observer"),
        HITS[0][0],
        settings=SourcesSettings(web_fetch_max_content_tokens=4000),
        now=NOW,
    )
    assert snapshot.text == PAGE_TEXT and snapshot.title == "Toma de la Bastilla"
    assert snapshot.retrieved_at == "2026-09-25T10:00:00Z" and snapshot.fetched_at == NOW
    request = fake.requests[0]
    assert request.tools == [
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": 1,
            "max_content_tokens": 4000,
        }
    ]
    assert HITS[0][0] in request.messages[0]["content"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"error_code": "url_not_accessible"}, "url_not_accessible"),
        ({"media_type": "application/pdf", "data": "JVBERi0="}, "impórtalo como PDF"),
        ({"text": "   "}, "vacía"),
    ],
)
async def test_snapshot_refuses_what_is_not_a_text_page(kwargs: dict, message: str) -> None:
    fake = fetch_reply(FakeClaude(), **({"text": "x"} | kwargs))
    with pytest.raises(WebFetchError, match=message):
        await snapshot_page(fake.client("observer"), HITS[0][0])


async def test_snapshot_refuses_a_non_web_url() -> None:
    fake = FakeClaude()
    with pytest.raises(WebFetchError):
        await snapshot_page(fake.client("observer"), "file:///etc/passwd")
    assert fake.requests == []


async def test_keep_stores_a_markdown_snapshot(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    snapshot = await snapshot_page(
        fetch_reply(FakeClaude(), title="Toma de la Bastilla").client("observer"),
        HITS[0][0],
        now=NOW,
    )
    search = await search_web(search_reply(FakeClaude()).client("observer"), "bastilla")
    kept = keep_snapshot(
        tmp_vault,
        *topic,
        snapshot,
        result=search.results[0],
        search_id="ws-1",
        query="bastilla",
        session_id="20260925-100000",
    )
    assert kept.source_id == "sources/web/001-toma-de-la-bastilla.md"
    stored = read_source(tmp_vault, kept.path.relative_to(tmp_vault.path).as_posix())
    text = stored.content.decode()
    assert text.startswith("# Toma de la Bastilla\n\n> Copia de <https://es.wikipedia.org/")
    assert "descargada el 2026-09-25" in text and PAGE_TEXT in text
    meta = stored.meta or {}
    assert meta["url"] == HITS[0][0] and meta["external"] is True
    assert meta["query"] == "bastilla" and meta["search_id"] == "ws-1"
    assert meta["kept_by"] == "student" and meta["summary"].startswith("Resumen de")
    assert str(meta["fetched_at"]).startswith("2026-09-25T10:30")
    assert [s.kind for s in list_sources(tmp_vault, *topic)] == ["web"]


async def test_keep_refuses_a_page_with_a_key(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    key = "sk-ant-api03-" + "a" * 90
    snapshot = await snapshot_page(
        fetch_reply(FakeClaude(), text=f"clave {key}").client("observer"), HITS[0][0]
    )
    with pytest.raises(SecretRefused):
        keep_snapshot(tmp_vault, *topic, snapshot)
    assert list_sources(tmp_vault, *topic) == []


async def test_search_log_folds_into_records(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    search = await search_web(search_reply(FakeClaude()).client("observer"), "bastilla")
    first, second, third = new_search_id(NOW), new_search_id(NOW), "ws-huerfana"
    assert first != second and first.startswith("ws-20260925-103000-")
    record_queued(
        tmp_vault,
        *topic,
        search_id=first,
        query="bastilla",
        requested_by="voice",
        session_id="20260925-100000",
        now=NOW,
    )
    record_results(tmp_vault, *topic, search_id=first, search=search, now=NOW)
    kept = KeptWebSource(path=tmp_vault.path, source_id="sources/web/001-x.md", title="X", url="u")
    record_kept(tmp_vault, *topic, search_id=first, index=0, kept=kept, kept_by="student", now=NOW)
    later = NOW.replace(minute=40)
    record_queued(
        tmp_vault, *topic, search_id=second, query="luis xvi", requested_by="web", now=later
    )
    record_failed(tmp_vault, *topic, search_id=second, reason="cost_cap", message="tope", now=later)
    record_failed(tmp_vault, *topic, search_id=third, reason="error", message="?", now=later)

    newest, oldest = list_web_searches(tmp_vault, *topic)
    assert (newest.search_id, newest.status, newest.reason) == (second, "failed", "cost_cap")
    assert newest.requested_by == "web" and newest.session_id is None
    assert (oldest.search_id, oldest.status, oldest.requested_by) == (first, "done", "voice")
    assert [r.url for r in oldest.results] == [HITS[0][0], HITS[1][0]]
    assert [(k.index, k.source_id) for k in oldest.kept] == [(0, "sources/web/001-x.md")]
    assert oldest.session_id == "20260925-100000" and oldest.model == "claude-sonnet-5"
