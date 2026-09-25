"""Web search as sources: "busca esto en Internet" -> offered pages -> kept Markdown snapshots.

Two Claude calls, both through `studentassistant.llm` with its server-side web tools:

1. `search_web` sends the request with the web search tool and a strict `offer_results` tool
   (prompt `prompts/web_search.md`): Claude searches and offers at most `web_search_max_results`
   pages (`url`, `title`, a Spanish `summary`, `relevant`). Nothing is stored as a source yet.
2. `snapshot_page` sends one offered URL with the web fetch tool only (prompt
   `prompts/web_fetch.md`) and takes the fetched document's text from the `web_fetch_tool_result`
   block -- the page as the fetch tool returned it, never Claude's rewording of it.
   `keep_snapshot` stores it through `vault.put_source` as `sources/web/NNN-<slug>.md` (a Markdown
   header naming the title, the URL and the fetch date, then the text) with a `.yaml` sidecar
   (`url`, `title`, `fetched_at`, `retrieved_at`, `query`, `search_id`, `summary`, `kept_by`, ...).

What was asked and offered is kept per topic in `conversations/web-search.jsonl` (records
`search.queued`, `search.results`, `search.failed`, `search.kept`, each with a `detail`), so the
review UI can list a topic's searches and keep results after the session that asked for them has
ended: `list_web_searches` folds it. The records never hold the search results' encrypted content
nor the fetched pages' text (the snapshot is the source file).
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.config import SourcesSettings
from studentassistant.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    RefusalError,
    load_prompt,
    parse_web_results,
    run_server_tools,
    strict_tool,
    web_fetch_tool,
    web_search_tool,
)
from studentassistant.vault import (
    ConversationRecord,
    Vault,
    append_conversation_record,
    list_sources,
    put_source,
    read_conversation,
)

SEARCH_PROMPT = "web_search"
FETCH_PROMPT = "web_fetch"
OFFER_TOOL = "offer_results"
CONVERSATION = "web-search"

QUEUED = "search.queued"
RESULTS = "search.results"
FAILED = "search.failed"
KEPT = "search.kept"

# Session events (ADR-0003 envelope, origin `observer`): what the observer and the editor read.
WEB_SEARCH_RESULTS_KIND = "web.search_results"
WEB_SEARCH_FAILED_KIND = "web.search_failed"
WEB_SNAPSHOT_STORED_KIND = "web.snapshot_stored"

RequestedBy = Literal["voice", "web", "editor"]
KeptBy = Literal["student", "assistant", "editor"]
AddedVia = Literal["url", "share"]
"""How a page given by its address arrived: pasted in the web UI, or shared from the phone."""
FailureReason = Literal["cost_cap", "refused", "error", "interrupted"]

_HTTP_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_FALLBACK_TITLE = "pagina web"


class WebSearchError(LLMError):
    """A search that gave no usable answer; the message is Spanish, for the student."""


class WebFetchError(LLMError):
    """A page that could not be fetched as text; the message is Spanish, for the student."""


class OfferedResult(BaseModel):
    """One page Claude offers (the `offer_results` tool input item)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    summary: str
    relevant: bool


class OfferedResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[OfferedResult]


class WebResult(BaseModel):
    """One offered page as stored and served: `found_in_search` is false when its URL is not
    among the pages the search tool returned (Claude should never invent one)."""

    url: str
    title: str
    summary: str = ""
    relevant: bool = False
    found_in_search: bool = True


@dataclass
class WebSearch:
    """The outcome of `search_web`."""

    query: str
    results: list[WebResult]
    queries: list[str]
    responses: list[LLMResponse]
    prompt_hash: str

    @property
    def model(self) -> str:
        return self.responses[-1].model if self.responses else ""


@dataclass
class WebSnapshot:
    """A fetched page (`snapshot_page`)."""

    url: str
    requested_url: str
    title: str
    text: str
    retrieved_at: str | None
    fetched_at: datetime
    media_type: str | None
    responses: list[LLMResponse] = field(default_factory=list)


def new_search_id(now: datetime | None = None) -> str:
    """`ws-YYYYMMDD-HHMMSS-<hex>`: unique, sortable, a protocol `Id`."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"ws-{moment:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def is_http_url(url: str) -> bool:
    return bool(_HTTP_URL.match(url.strip()))


# -- 1. search -------------------------------------------------------------------------------------


def search_request_text(
    query: str, *, max_results: int, subject: str | None = None, topic: str | None = None
) -> str:
    lines = []
    if subject:
        lines.append(f"Asignatura: {subject}")
    if topic:
        lines.append(f"Tema: {topic}")
    lines.append(f"Búsqueda pedida: {query}")
    lines.append(f"Ofrece como mucho {max_results} páginas.")
    return "\n".join(lines)


def _offered(response: LLMResponse) -> tuple[OfferedResults | None, str]:
    if response.stop_reason == "refusal":
        raise RefusalError("Claude declined the web search")
    calls = [call for call in response.tool_calls if call.name == OFFER_TOOL]
    if not calls:
        return None, f"you did not call the `{OFFER_TOOL}` tool"
    try:
        return OfferedResults.model_validate(json.loads(calls[0].input_json)), ""
    except (json.JSONDecodeError, ValidationError) as error:
        return None, f"the `{OFFER_TOOL}` input is not valid: {error}"


async def search_web(
    client: LLMClient,
    query: str,
    *,
    settings: SourcesSettings | None = None,
    subject: str | None = None,
    topic: str | None = None,
) -> WebSearch:
    """Search the web for `query` and return the pages Claude offers (nothing is stored).

    Raises `RefusalError` on a refusal, `WebSearchError` when no `offer_results` call came after
    one re-ask, and any `LLMError` of the client (a reached cost cap included).
    """
    settings = settings or SourcesSettings()
    query = query.strip()
    if not query:
        raise WebSearchError("La búsqueda está vacía.")
    prompt = load_prompt(SEARCH_PROMPT)
    tools = [
        web_search_tool(client.llm_settings, max_uses=settings.web_search_max_uses),
        strict_tool(OFFER_TOOL, "Offer the pages found for the student's request.", OfferedResults),
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": search_request_text(
                query, max_results=settings.web_search_max_results, subject=subject, topic=topic
            ),
        }
    ]
    run = await run_server_tools(
        client, messages, system=prompt.content, tools=tools, prompt_hash=prompt.hash
    )
    responses = list(run.responses)
    offered, reason = _offered(run.final)
    if offered is None:
        retry = [
            *run.messages,
            run.final.assistant_turn(),
            {
                "role": "user",
                "content": f"{reason}. Call `{OFFER_TOOL}` now with the pages you found.",
            },
        ]
        again = await run_server_tools(
            client, retry, system=prompt.content, tools=tools, prompt_hash=prompt.hash
        )
        responses.extend(again.responses)
        offered, reason = _offered(again.final)
        if offered is None:
            raise WebSearchError("La búsqueda no devolvió resultados utilizables.")
    found = parse_web_results([block for r in responses for block in r.content])
    return WebSearch(
        query=query,
        results=_clean_results(offered.results, found.hits, settings.web_search_max_results),
        queries=found.queries,
        responses=responses,
        prompt_hash=prompt.hash,
    )


def _clean_results(
    offered: Sequence[OfferedResult], hits: Sequence[Any], limit: int
) -> list[WebResult]:
    """Offered pages with an http(s) URL, each once, at most `limit`, titled from the search."""
    titles = {hit.url: hit.title for hit in hits}
    results: list[WebResult] = []
    seen: set[str] = set()
    for item in offered:
        url = item.url.strip()
        if not is_http_url(url) or url in seen:
            continue
        seen.add(url)
        results.append(
            WebResult(
                url=url,
                title=item.title.strip() or titles.get(url) or url,
                summary=item.summary.strip(),
                relevant=item.relevant,
                found_in_search=url in titles,
            )
        )
        if len(results) == limit:
            break
    return results


# -- 2. snapshot -----------------------------------------------------------------------------------


async def snapshot_page(
    client: LLMClient,
    url: str,
    *,
    settings: SourcesSettings | None = None,
    now: datetime | None = None,
) -> WebSnapshot:
    """Fetch `url` with Claude's web fetch tool and return its text (nothing is stored).

    Raises `WebFetchError` (Spanish) when the URL is not http(s), the fetch failed, or the page is
    not text (a PDF: import it as a PDF instead); `RefusalError`; any `LLMError` of the client.
    """
    settings = settings or SourcesSettings()
    url = url.strip()
    if not is_http_url(url):
        raise WebFetchError("Esa dirección no es una página web (http o https).")
    prompt = load_prompt(FETCH_PROMPT)
    tools = [
        web_fetch_tool(
            client.llm_settings,
            max_uses=1,
            max_content_tokens=settings.web_fetch_max_content_tokens,
        )
    ]
    run = await run_server_tools(
        client,
        [{"role": "user", "content": f"Descarga esta página: {url}"}],
        system=prompt.content,
        tools=tools,
        prompt_hash=prompt.hash,
    )
    if run.final.stop_reason == "refusal":
        raise RefusalError("Claude declined to fetch the page")
    found = parse_web_results(run.content)
    documents = [doc for doc in found.documents if doc.url == url] or found.documents
    if not documents:
        code = found.fetch_errors[0] if found.fetch_errors else "no_result"
        raise WebFetchError(f"No se pudo descargar la página ({code}).")
    document = documents[0]
    if document.text is None:
        if document.media_type == "application/pdf":
            raise WebFetchError(
                "La página es un PDF: descárgalo e impórtalo como PDF para guardarlo como fuente."
            )
        raise WebFetchError(f"La página no es texto ({document.media_type or 'desconocido'}).")
    if not document.text.strip():
        raise WebFetchError("La página descargada está vacía.")
    return WebSnapshot(
        url=document.url or url,
        requested_url=url,
        title=(document.title or "").strip(),
        text=document.text,
        retrieved_at=document.retrieved_at,
        fetched_at=(now or datetime.now(UTC)).astimezone(UTC),
        media_type=document.media_type,
        responses=list(run.responses),
    )


def render_snapshot(snapshot: WebSnapshot, title: str) -> str:
    """The stored Markdown: title, where and when it was copied from, then the page's text."""
    date = snapshot.fetched_at.strftime("%Y-%m-%d")
    body = snapshot.text.strip()
    return f"# {title}\n\n> Copia de <{snapshot.url}>, descargada el {date}.\n\n{body}\n"


@dataclass(frozen=True)
class KeptWebSource:
    """A stored snapshot: its vault path, its `source_id` (`sources/web/NNN-<slug>.md`), title."""

    path: Path
    source_id: str
    title: str
    url: str


def keep_snapshot(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    snapshot: WebSnapshot,
    *,
    result: WebResult | None = None,
    search_id: str | None = None,
    query: str | None = None,
    kept_by: KeptBy = "student",
    session_id: str | None = None,
    added_via: AddedVia | None = None,
) -> KeptWebSource:
    """Store `snapshot` as `sources/web/NNN-<slug>.md` with its sidecar (blocking).

    `added_via` is set for a page the student gave by its address (`url`, `share`) rather than
    kept from a search.

    Raises the vault's `SecretRefused` when the page looks like it carries a key (nothing is
    written) and its topic errors for an unknown topic.
    """
    title = snapshot.title or (result.title if result else "") or snapshot.url
    name = title if re.search(r"[A-Za-z0-9]", title) else _FALLBACK_TITLE
    meta: dict[str, Any] = {
        "url": snapshot.url,
        "title": title,
        "fetched_at": snapshot.fetched_at,
        "retrieved_at": snapshot.retrieved_at,
        "media_type": snapshot.media_type,
        "external": True,
        "kept_by": kept_by,
    }
    if snapshot.requested_url != snapshot.url:
        meta["requested_url"] = snapshot.requested_url
    if query:
        meta["query"] = query
    if search_id:
        meta["search_id"] = search_id
    if result is not None and result.summary:
        meta["summary"] = result.summary
    if session_id:
        meta["session"] = session_id
    if added_via:
        meta["added_via"] = added_via
    path = put_source(
        vault,
        subject_slug,
        topic_slug,
        "web",
        name,
        render_snapshot(snapshot, title),
        meta,
    )
    return KeptWebSource(
        path=path, source_id=f"sources/web/{path.name}", title=title, url=snapshot.url
    )


def find_kept_url(
    vault: Vault, subject_slug: str, topic_slug: str, url: str
) -> KeptWebSource | None:
    """The topic's web snapshot of `url` (its sidecar's `url` or `requested_url`), if stored.

    Blocking; raises the vault's topic errors for an unknown topic.
    """
    wanted = url.strip()
    for source in list_sources(vault, subject_slug, topic_slug):
        meta = source.meta or {}
        if source.kind != "web" or wanted not in (meta.get("url"), meta.get("requested_url")):
            continue
        path = vault.path / source.path
        return KeptWebSource(
            path=path,
            source_id=f"sources/web/{path.name}",
            title=str(meta.get("title") or meta.get("url") or wanted),
            url=str(meta.get("url") or wanted),
        )
    return None


# -- the topic's search log ---------------------------------------------------------------------


class KeptResult(BaseModel):
    index: int
    url: str
    source_id: str
    kept_by: str


class WebSearchRecord(BaseModel):
    """One search of a topic as `list_web_searches` folds it."""

    search_id: str
    query: str
    requested_by: str
    session_id: str | None = None
    queued_at: datetime
    status: Literal["queued", "done", "failed"] = "queued"
    results: list[WebResult] = Field(default_factory=list)
    reason: str | None = None
    message: str | None = None
    model: str | None = None
    kept: list[KeptResult] = Field(default_factory=list)


def _append(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    kind: str,
    detail: dict[str, Any],
    *,
    now: datetime | None = None,
    response: LLMResponse | None = None,
    prompt_hash: str | None = None,
) -> None:
    append_conversation_record(
        vault,
        subject_slug,
        topic_slug,
        CONVERSATION,
        ConversationRecord(
            time=now or datetime.now(UTC),
            kind=kind,
            detail=detail,
            model=response.model if response else None,
            prompt_hash=prompt_hash,
            usage=response.usage.model_dump() if response else None,
        ),
    )


def record_queued(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    search_id: str,
    query: str,
    requested_by: RequestedBy,
    session_id: str | None = None,
    now: datetime | None = None,
) -> None:
    _append(
        vault,
        subject_slug,
        topic_slug,
        QUEUED,
        {
            "search_id": search_id,
            "query": query,
            "requested_by": requested_by,
            "session_id": session_id,
        },
        now=now,
    )


def record_results(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    search_id: str,
    search: WebSearch,
    now: datetime | None = None,
) -> None:
    usage: dict[str, int] = {}
    for response in search.responses:
        for key, value in response.usage.model_dump().items():
            usage[key] = usage.get(key, 0) + value
    last = search.responses[-1] if search.responses else None
    append_conversation_record(
        vault,
        subject_slug,
        topic_slug,
        CONVERSATION,
        ConversationRecord(
            time=now or datetime.now(UTC),
            kind=RESULTS,
            detail={
                "search_id": search_id,
                "queries": search.queries,
                "results": [result.model_dump() for result in search.results],
                "calls": len(search.responses),
            },
            model=last.model if last else None,
            prompt_hash=search.prompt_hash,
            usage=usage or None,
        ),
    )


def record_failed(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    search_id: str,
    reason: FailureReason,
    message: str,
    now: datetime | None = None,
) -> None:
    _append(
        vault,
        subject_slug,
        topic_slug,
        FAILED,
        {"search_id": search_id, "reason": reason, "message": message},
        now=now,
    )


def record_kept(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    *,
    search_id: str,
    index: int,
    kept: KeptWebSource,
    kept_by: KeptBy,
    now: datetime | None = None,
) -> None:
    _append(
        vault,
        subject_slug,
        topic_slug,
        KEPT,
        {
            "search_id": search_id,
            "index": index,
            "url": kept.url,
            "source_id": kept.source_id,
            "kept_by": kept_by,
        },
        now=now,
    )


def list_web_searches(vault: Vault, subject_slug: str, topic_slug: str) -> list[WebSearchRecord]:
    """Every search of the topic, newest first, folded from `conversations/web-search.jsonl`.

    A record naming a search with no `search.queued` before it is ignored, as is a malformed one.
    """
    searches: dict[str, WebSearchRecord] = {}
    for record in read_conversation(vault, subject_slug, topic_slug, CONVERSATION):
        detail = record.detail or {}
        search_id = detail.get("search_id")
        if not isinstance(search_id, str):
            continue
        try:
            if record.kind == QUEUED:
                searches[search_id] = WebSearchRecord(
                    search_id=search_id,
                    query=str(detail.get("query") or ""),
                    requested_by=str(detail.get("requested_by") or "web"),
                    session_id=detail.get("session_id"),
                    queued_at=record.time,
                )
                continue
            search = searches.get(search_id)
            if search is None:
                continue
            if record.kind == RESULTS:
                search.status = "done"
                search.results = [WebResult.model_validate(r) for r in detail.get("results", [])]
                search.model = record.model
            elif record.kind == FAILED:
                search.status = "failed"
                search.reason = str(detail.get("reason") or "error")
                search.message = str(detail.get("message") or "")
            elif record.kind == KEPT:
                kept = KeptResult.model_validate(detail)
                search.kept = [k for k in search.kept if k.index != kept.index] + [kept]
        except (ValidationError, TypeError, ValueError):
            continue
    return sorted(searches.values(), key=lambda s: (s.queued_at, s.search_id), reverse=True)


def find_web_search(
    vault: Vault, subject_slug: str, topic_slug: str, search_id: str
) -> WebSearchRecord | None:
    for search in list_web_searches(vault, subject_slug, topic_slug):
        if search.search_id == search_id:
            return search
    return None


__all__ = [
    "CONVERSATION",
    "WEB_SEARCH_FAILED_KIND",
    "WEB_SEARCH_RESULTS_KIND",
    "WEB_SNAPSHOT_STORED_KIND",
    "AddedVia",
    "KeptResult",
    "KeptWebSource",
    "OfferedResult",
    "OfferedResults",
    "WebFetchError",
    "WebResult",
    "WebSearch",
    "WebSearchError",
    "WebSearchRecord",
    "WebSnapshot",
    "find_kept_url",
    "find_web_search",
    "is_http_url",
    "keep_snapshot",
    "list_web_searches",
    "new_search_id",
    "record_failed",
    "record_kept",
    "record_queued",
    "record_results",
    "render_snapshot",
    "search_web",
    "search_request_text",
    "snapshot_page",
]
