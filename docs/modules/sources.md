# Module: sources

**Lives in:** `backend/src/studentassistant/sources/`.

## Responsibility
- Capture processing: pick the sharpest still of a burst (Laplacian variance), downscale, page
  crop/deskew, store both through `vault`, link to the transcript window around the capture.
- Page transcription (role `transcriber`, vision): page -> Markdown keeping structure, schemes as
  nested lists or mermaid, uncertain words as `[[?word]]`, using spoken hints around the capture
  time; uncertain words become pending items.
- Textbook pages (printed-text prompt, page-number detection), PDF import (page ranges, PyMuPDF
  text), web search and URL snapshots (Markdown + URL + fetch date).

## Boundaries
- Claude only via `llm`; storage only via `vault`.

## Public surface (`from studentassistant.sources import ...`)

What exists today, after issues #34, #44, #50, #58 and #59: PDF import, capture processing,
page transcription, textbook pages and web search.

### Capture processing -- `captures.py`
- `store_capture(vault, subject_slug, topic_slug, kind, stills, meta, session_t_ms, settings)
  -> StoredCapture` processes one burst (`stills`: `BurstStill(data, content_type)` in burst
  order) and stores it through one `vault.put_source` call as a page of `sources/<kind>/`:
  - `page-NNN.jpg`: the sharpest still (variance of the Laplacian on a grayscale copy at a common
    1200 px long edge; ties go to the frame nearest the middle, `len // 2`, the earlier one when
    two are equally near), downscaled to `capture_long_edge` and re-encoded as JPEG at
    `capture_jpeg_quality`. Stills that do not decode are skipped; if none decodes,
    `CaptureImageError` (Spanish message) and nothing is written.
  - `page-NNN.page.jpg`: the page image. The largest convex quadrilateral covering 20-98 % of
    the still, with at most one corner on the image border and clearly lighter than what surrounds
    it (Canny edges and an Otsu threshold both searched), is warped flat by a perspective
    transform; then a mild CLAHE contrast boost on lightness. No page found: the uncropped still
    with the same contrast boost.
  - `page-NNN.burst<K>.<ext>`: every other still exactly as uploaded, `K` its 1-based position in
    the burst (the retention purge's `burst-original` target).
  - `page-NNN.yaml`: `meta` plus `width_px`/`height_px` (of `page-NNN.jpg`), `session_t_ms`,
    `transcript_window: {t_start, t_end}` (session ms; `capture_window_before_seconds` before to
    `capture_window_after_seconds` after, never below 0) -- the span of `transcript.jsonl` a page
    transcription reads as spoken hints -- `selected_image` (the kept still's `K`), `sharpness`
    (per still, `null` for one that did not decode) and `page_detected`.
  `StoredCapture` has `path` (`page-NNN.jpg`), `page_path` and `processed`.
- `process_burst(stills: Sequence[bytes], settings) -> ProcessedBurst` (`selected`, `sharpness`,
  `still`, `page`, `page_detected`, `width_px`, `height_px`) is the same processing without
  storing; `transcript_window(session_t_ms, settings) -> {"t_start", "t_end"}`.
- The building blocks (`decode_image`, `sharpness`, `pick_sharpest`, `downscale`, `find_page`,
  `crop_page`, `enhance_contrast`) are importable from `studentassistant.sources.captures`.
- Everything is CPU-bound (OpenCV, `opencv-python-headless`): the capture upload
  (`server/captures.py`) runs `store_capture` in a worker thread.

### Page transcription -- `transcription.py`, `transcriber.py`
Not re-exported by the package root (they import `studentassistant.llm`): import them from their
submodules.

- Prompt `prompts/page_transcription.md` (role `transcriber`, `[llm.roles.transcriber]`, Sonnet by
  default): Markdown in the page's language (Spanish), headings, nested lists, `$...$` formulas,
  tables, arrows and schemes as nested lists with `→` or a `mermaid` block, figures as
  `> [Figura: ...]`; a doubtful word as `[[?word]]`, an illegible one as `[[?]]`; only the
  Markdown, no fence. Speech is a hint for reading words, never copied into the page.
- `read_page_input(vault, subject, topic, session_id, source_path, page_path, settings,
  extra_segments=()) -> PageInput` (blocking): the page image (`page-NNN.page.jpg`); the original
  still as well when the crop is doubtful (`crop_is_doubtful`: a page was detected but covers less
  than `transcription_min_crop_share` of the still; with no page detected the page image is the
  whole still already); the hints (`hint_segments`: the session's `transcript.jsonl` segments,
  plus `extra_segments` not written yet, overlapping the sidecar's `transcript_window`); subject
  name, topic title, source kind and page number.
- `request_content(page)`: the image block(s) (base64 JPEG) first, then the text
  (`render_request_text`: subject, topic, source and page, the capture time and the hints as
  `- [start-end s] text`). `transcribe_page(client, page, vault, source_path) ->
  PageTranscription` (`text`, `path`, `uncertain`, `page_input`, `response`, `prompt_hash`) sends
  one request (system = the prompt, cached; no tools), removes a fence wrapped around the whole
  answer (`clean_markdown`), and stores it as `page-NNN.md` through
  `vault.put_page_transcription`. `RefusalError` on a refusal, `TranscriptionError` (an
  `LLMError`) on an empty answer; nothing is written then.
- `find_uncertain(text) -> [UncertainWord(word | None, line, line_number, column)]` lists the
  `[[?...]]` marks; `pending_ops(uncertain, session_id=, capture_id=, source_kind=,
  page_number=)` makes one observer `AddPending` per mark: `pending_id`
  `ill-<session>-<capture>-<n>`, kind `illegible`, `capture_ids` `[capture_id]` (the page ref), a
  Spanish `text` naming the word, the page, the mark's line and column, and the line. The line and
  column keep two marks of one page apart in the pending queue, which never merges texts naming
  different numbers (`observer.pending.is_duplicate`); the same page transcribed again merges.
- `PageTranscriber(bus, lookup, *, settings, client_factory, on_write=None)` subscribes to the
  session bus (`start()`/`stop()`; `stop()` cancels what is still running). For each persisted
  `capture.stored` (payload `capture_id`, `source_path`, `page_path`; a repeated id is ignored) it
  starts a job that:
  1. waits until the capture's `transcript_window.t_end` (from the sidecar) has passed plus
     `transcription_grace_seconds`, so the speech after the photo counts too;
  2. takes one of `transcription_concurrency` slots, reads the page (hints include the
     `transcript.final` events seen on the bus) and calls `transcribe_page` with the session's
     `transcriber` client (`default_client_factory(settings, transport)`: bound to the session's
     `LedgerBinding`, so capped and recorded);
  3. calls `on_write()` (the server's `SessionService.note_change`), appends the exchange to
     `conversations/transcriber-<session>.jsonl` (images as `{"type": "vault", "path"}`, never
     their bytes), publishes one `observer.state_op` `add_pending` per mark, then
     `page.transcribed` (`observer.context.PAGE_TRANSCRIPTION_KIND`) with `capture_id`, `text`,
     `path` (the `.md`), `source_path`, `page_path`, `source_context`, `page_number`,
     `original_sent`, `hint_segments`, `uncertain`, `pending_ids`, `model`, `prompt_hash`,
     `attempts`. All of them have origin `observer` (ADR-0003 has no `sources` origin).
  A failed attempt (an `LLMError`, a vault or OS error) is retried up to `transcription_attempts`
  times, `transcription_retry_seconds` apart, doubling. A reached cost cap (nothing sent) or a
  refusal is not retried. When a page is given up, `page.transcription_failed` (`capture_id`,
  `reason` `cost_cap` | `refused` | `error`, `message`, `attempts`) is published. A job never
  blocks the bus, the capture or other jobs; a publish that fails (the session ended) is logged.
  `flush(session_id)` ends the session's waits and waits for its jobs (shielded, so a hook
  timeout never cancels a call); `wait_idle(session_id)` waits without hurrying; `drain()` waits
  until delivered events are handled.
- Server: `create_app` builds one when it gets an `llm_transport` (only `serve` passes the real
  one) and `[sources] transcription_enabled`, and registers `flush` with
  `SessionService.add_before_ended` before the observer's, so the observer sees the
  transcriptions before `session.ended`. `catch_up_vault` is registered with
  `SessionService.add_on_open` for the server-start catch-up below.

### Textbook pages -- `transcription.py` (#58)
- A capture taken while the session's source context is `book` (the `switch_source` button, or
  the voice command once #47 maps it to the same event) is stored under `sources/book/` by
  `store_capture`, like a notes page. Its transcription differs in three ways:
  1. Prompt `prompts/page_transcription_book.md` (`prompt_name(kind)`: `BOOK_PROMPT_NAME` for
     `book`, `PROMPT_NAME` otherwise): printed text -- headings without the running header and
     footer, paragraphs, bold/italics, panels as `> **Título:** ...`, exercises as numbered lists,
     figures with their caption --, the same `[[?word]]`/`[[?]]` marks, and a last line
     `<!-- página impresa: 83 -->` (`ninguna` when no number is printed or readable; the page
     filling most of the photo when two show). The request names the topic's book title
     (`vault.get_book`) and calls the stored number a photo number, not a page.
  2. The page number: `split_printed_page(text) -> (text, printed | None)` takes that line off
     the Markdown (only for `book`); `spoken_page_number(hints, session_t_ms)` reads "página 83",
     "pág. 83", "página número 7", "la página ochenta y tres" (Spanish number words up to 9999)
     from the hint segments -- the segment nearest the capture time, the last page it names.
     `BookPage(printed, spoken)` keeps the printed one first (`number_from` `image`), else the
     spoken one (`speech`). `transcribe_page` writes `book_page`, `book_page_from`,
     `book_page_printed`, `book_page_spoken` to the page's sidecar (`vault.update_page_meta`)
     after the Markdown and returns it as `PageTranscription.book_page`.
  3. Events and citations: `page.transcribed` of a book page adds `book_page` and
     `book_page_from` (a recorded-again page reads them from the sidecar); its pending items name
     `la página 83 del libro` (`pending_ops(..., book_page=)`), `la foto N del libro` when the
     number is unknown. The editor cites it as `Libro «<título>», página 83` (`editor.md`).
- The book title per topic: `vault.set_book` / `get_book` (`sources/book/book.yaml`), set from
  the web with `PUT /api/subjects/{s}/topics/{t}/book` (`server.md`).

### Catch-up of owed pages -- `catchup.py` (#181)
- A page is *owed* when a session of the topic stored it (`capture.stored`) and no
  `page.transcribed` names it -- by `capture_session_id` (the session that stored the capture;
  the event's own session when absent) and `capture_id` -- nor a `page.transcription_failed`
  with reason `refused`. `owed_pages(events, *, before=None, sessions=None) -> (pages,
  pending_ids)` is pure (`PageRef`: `session_id`, `capture_id`, `source_path`, `page_path`, `t`,
  `source_kind`); `read_owed(vault, subject, topic, *, before, sessions) -> Owed` splits them into
  `to_transcribe` (no `page-NNN.md`, `transcription_path(source_path)`) and `to_record` (the
  Markdown is stored, the events are not) and returns every `add_pending` id already in the log.
- When: at every `session.started` / `session.resumed` (every session of the topic, captures
  stored before the opening event), and once at server start (`catch_up_vault(vault)`, called when
  the server first opens the vault: each topic's unended study sessions and its newest one, review sessions left out;
  `wait_startup()` waits for it). Owed pages are transcribed at once (no window wait; hints from the capture's own session's
  `transcript.jsonl`), bound to the ledger of the session that queued them; the exchange goes to
  `conversations/transcriber-<capture session>.jsonl`. Stored ones have their events published
  from the Markdown with `recovered: true`. A page a job is already working on is skipped.
- Where events go (the convention for a page transcribed after its session ended): to the page's
  own session while it is live, else to the live session of the same topic, else they wait -- the
  next start or resume of the topic finds the page owed and records it. So a late page's
  `add_pending` ops and `page.transcribed` land in a later `events.jsonl` of the same topic, which
  the observer folds (and catches up, #176) and the editor reads through the fold; the Markdown is
  in `sources/` from the start. Unlike `notes.generated` (#61), which is only kept in
  `conversations/editor.jsonl` when no session is active, these events are state the fold needs
  (pending items), so they are deferred rather than dropped. `page.transcribed` and
  `page.transcription_failed` always carry `capture_session_id`; an `add_pending` id already in the
  topic's log is never published again (the fold refuses a duplicate id).

### PDF import -- `pdf.py`
- `import_pdf(vault, subject_slug, topic_slug, name, content, *, pages=None, settings=None,
  now=None) -> ImportedPdf` stores the PDF `content` (called `name`), or only `pages` (a
  `PageRange(first, last)`, 1-based and inclusive, in the original's numbering), through
  `vault.put_source` as `sources/pdf/page-NNN.pdf`: a new PDF holding just the kept pages. Next to
  it, per kept page `K` (counted in the stored file), `page-NNN.pKKK.txt` (PyMuPDF text; empty for
  a scanned page) and `page-NNN.pKKK.jpg` (thumbnail, long edge `pdf_thumbnail_long_edge`, JPEG
  `pdf_thumbnail_quality`). The sidecar holds `original_name`, `imported_at`, `original_sha256`,
  `original_page_count`, `first_page`, `last_page`, `page_count`. `ImportedPdf` has `path`,
  `source_id` (`sources/pdf/page-NNN.pdf`), `meta` and `pages` (`ImportedPage`: `page`,
  `original_page`, `source_id` `...#page=K`, `text_path`, `image_path`, `has_text`).
- `parse_page_range(text) -> PageRange` reads `82-94`, `82–94`, `páginas 82 a 94`, `pág. 7`.
- Refusals (all `PdfImportError`, Spanish messages, nothing written): `PdfTooLargeError`,
  `PdfUnreadableError` (not a PDF, empty, or password-protected), `PageRangeError` (malformed,
  backwards or outside the PDF). A page text or the PDF that looks like a key raises the vault's
  `SecretRefused`.
- Page numbers: provenance cites `sources/pdf/page-NNN.pdf#page=K` with `K` in the stored file --
  what the link opens on GitHub and what Claude's citations count. `original_page(meta, K)` is the
  page the student knows (`first_page + K - 1`). `pdf_page_count(vault, s, t, source_id)` and
  `pdf_has_page(vault, s, t, source_id)` read the sidecar; the editor's `topic_source_resolver`
  uses the latter so a cited page outside the import is reported.
- Editor input: `pdf_document_block(vault, s, t, source_id, *, cache=False) -> dict` is a Claude
  `document` block (base64 `application/pdf`, `title` = original name, `context` naming the kept
  original pages, `citations: {enabled: true}`, `cache_control` with `cache=True`), to be placed
  in a user message before the question. `citation_source_id(source_id, citation)` maps a
  `page_location` citation of the answer to `sources/pdf/page-NNN.pdf#page=<start_page_number>`
  (`None` for other citation kinds).
- CLI: `studentassistant import-pdf <subject-slug>/<topic-slug> <file.pdf> [--pages 82-94]`
  imports into the configured vault and commits locally (`GitSync.checkpoint`, message
  `Importar PDF <file> en <subject>/<topic>`); the server's sync pushes it. Exit 1 with a Spanish
  message on any refusal, 2 on a malformed topic.

Import is CPU-bound (PyMuPDF): a server caller runs it in a worker thread, as the web upload
(`POST /api/subjects/{s}/topics/{t}/sources/pdf`, `server/pdf_upload.py`) does.

### Web search -- `web.py`, `web_searcher.py` (#59)
Not re-exported by the package root (they import `studentassistant.llm`): import them from their
submodules. "Busca esto en Internet" runs Claude's server-side web tools through
`studentassistant.llm` (`web_search_tool`, `web_fetch_tool`, `run_server_tools`; the versions come
from `[llm] web_search_tool` / `web_fetch_tool`, `*_20260209` by default).

- `search_web(client, query, *, settings, subject=None, topic=None) -> WebSearch` (`query`,
  `results`, `queries`, `responses`, `prompt_hash`, `model`): one request (prompt
  `prompts/web_search.md`, system cached) with the web search tool (`max_uses` =
  `web_search_max_uses`) and a strict `offer_results` tool (`OfferedResults`); `pause_turn` is
  resumed; no `offer_results` call is re-asked once, then `WebSearchError`. The offered pages are
  cleaned into `WebResult`s (`url`, `title`, Spanish `summary`, `relevant`, `found_in_search`: the
  URL is among the search tool's hits): http(s) only, each URL once, at most
  `web_search_max_results`. Nothing is stored. `RefusalError` on a refusal.
- `snapshot_page(client, url, *, settings, now=None) -> WebSnapshot` (`url`, `requested_url`,
  `title`, `text`, `retrieved_at`, `fetched_at`, `media_type`): one request (prompt
  `prompts/web_fetch.md`) with only the web fetch tool (`max_uses` 1, `max_content_tokens` =
  `web_fetch_max_content_tokens`); the text is the fetched document of the
  `web_fetch_tool_result` block, never Claude's rewording. `WebFetchError` (Spanish) for a non-web
  URL (nothing sent), a fetch error (its `error_code`), a PDF ("impórtalo como PDF") or another
  binary document, or an empty page.
- `keep_snapshot(vault, s, t, snapshot, *, result=None, search_id=None, query=None,
  kept_by="student", session_id=None) -> KeptWebSource` (`path`, `source_id`
  `sources/web/NNN-<slug>.md`, `title`, `url`) stores it through `vault.put_source` (kind `web`,
  the slug from the title): `# <title>`, `> Copia de <url>, descargada el YYYY-MM-DD.`, then the
  text; sidecar `url`, `title`, `fetched_at`, `retrieved_at`, `media_type`, `external: true`,
  `kept_by` (`student` | `assistant` | `editor`), and when known `requested_url`, `query`,
  `search_id`, `summary`, `session`. A page that looks like it carries a key: `SecretRefused`,
  nothing written. The editor reads it as any web snapshot and cites it
  `[Web: <título>](../sources/web/NNN-<slug>.md)`; the web UI marks those citations as external
  sources (green, "fuente externa").
- The topic's search log, `conversations/web-search.jsonl` (`ConversationRecord`s with a
  `detail`): `search.queued` (`search_id` `ws-YYYYMMDD-HHMMSS-<hex>`, `query`, `requested_by`
  `voice` | `web` | `editor`, `session_id`), `search.results` (`results`, `queries`, `calls`, plus
  `model`, `prompt_hash` and the summed `usage`), `search.failed` (`reason` `cost_cap` | `refused`
  | `error`, `message`), `search.kept` (`index`, `url`, `source_id`, `kept_by`); written with
  `record_queued` / `record_results` / `record_failed` / `record_kept`. It never holds the search
  results' encrypted content nor the pages' text. `list_web_searches(vault, s, t) ->
  [WebSearchRecord]` folds it, newest first (`status` `queued` | `done` | `failed`, `results`,
  `kept`, ...); `find_web_search(...)` picks one.
- `WebSearcher(bus, lookup, *, settings, client_factory, on_write=None)`: `start()` subscribes to
  `voice.command` events (the stt grammar's `web_search` command, payload `query`, #47) and
  queues a search of the session's topic; `submit(vault, s, t, query, *, requested_by="web",
  session_id=None) -> search_id` records `search.queued` and returns at once -- the search runs
  in the background, `web_search_concurrency` at a time, with a client of role
  `web_search_role` (`default_client_factory(settings, transport)`) bound to the topic's ledger
  and the asking session, so it is capped (a cap fails it, `reason: cost_cap`) and recorded; the
  web searches are priced at `[llm] web_search_usd_per_thousand`. At the end it records the
  results or the failure and, while the asking session is live, publishes
  `web.search_results` (`search_id`, `query`, `requested_by`, `results`, `model`) or
  `web.search_failed` (`search_id`, `query`, `reason`, `message`) on it, origin `observer`. With
  `web_auto_keep` the `relevant` results are kept at once (`kept_by: assistant`).
  `keep(vault, s, t, search_id, index, *, kept_by="student", session_id=None) -> KeptWebSource`
  fetches and stores one result, records `search.kept`, calls `on_write` and publishes
  `web.snapshot_stored` (`search_id`, `index`, `url`, `source_id`, `title`, `kept_by`) on the live
  session; keeping a result again returns the first snapshot. `UnknownSearchError` (404),
  `SearchNotDoneError` (409) and `KeepError` (422: not a text page, or a key) carry Spanish
  messages. `list(vault, s, t)` is `list_web_searches` with a `queued` search no job runs shown as
  failed, `reason: interrupted` (the server stopped). `stop()` cancels what is running;
  `wait_idle()` waits for every queued search.
- Server: `create_app` builds one when it gets an `llm_transport` and `[sources]
  web_search_enabled`; the routes are `server/web_search_routes.py`
  (`GET/POST /api/subjects/{s}/topics/{t}/web-searches`, `POST
  .../web-searches/{search_id}/results/{index}/keep`, see `docs/modules/server.md`). The web topic
  page's "Buscar en Internet" panel uses them.

### Size limit and storage policy
The vault keeps PDFs in plain git, never Git LFS (ADR-0002 leaves LFS an opt-in for later), and
the stored PDF is sent base64 (4/3 of its size) to Claude, whose requests are capped at 32 MB. So:

| `[sources]` key (`SA_SOURCES__*`) | default | beyond it |
|---|---|---|
| `max_pdf_bytes` | 200 MiB | the PDF is refused before it is opened (the CLI checks the file size before reading it) |
| `max_pdf_pages` | 100 | a range with more pages is refused, never truncated: choose a shorter range |
| `max_stored_pdf_bytes` | 20 MiB | the kept pages as a PDF are refused: choose a shorter range |
| `pdf_thumbnail_long_edge` | 1200 px | -- |
| `pdf_thumbnail_quality` | 85 | -- |
| `capture_long_edge` | 2400 px | a captured still and its page image are downscaled to it |
| `capture_jpeg_quality` | 85 | -- |
| `capture_window_before_seconds` | 20 | a capture's transcript window starts this long before it |
| `capture_window_after_seconds` | 10 | ...and ends this long after it |
| `transcription_enabled` | true | false: no page is transcribed |
| `transcription_concurrency` | 2 | pages sent to Claude at once; the rest wait |
| `transcription_attempts` | 3 | tries per page before `page.transcription_failed` |
| `transcription_retry_seconds` | 5 | first wait between tries, doubled each time |
| `transcription_grace_seconds` | 2 | extra wait after a capture's transcript window |
| `transcription_min_crop_share` | 0.3 | a detected page smaller than this share of the still also sends the original |
| `web_search_enabled` | true | false: no web search is run (the routes answer 503) |
| `web_search_role` | `observer` | the `[llm.roles.<role>]` that searches and fetches |
| `web_search_max_uses` | 3 | web searches Claude may run for one request |
| `web_search_max_results` | 5 | pages one search offers |
| `web_fetch_max_content_tokens` | 30000 | content a kept page is fetched with |
| `web_search_concurrency` | 1 | searches running at once; the rest wait |
| `web_auto_keep` | false | true: the pages Claude marks relevant are kept without asking |
