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

What exists today, after issues #34, #44, #50, #58, #59, #62 and #324: PDF import, capture
processing and triage, page transcription, textbook pages, web search and web pages by URL.

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
    (per still, `null` for one that did not decode), `page_detected` and `triage` (below).
  `StoredCapture` has `path` (`page-NNN.jpg`), `page_path`, `processed`, `triage` (the
  `TriageResult`, `None` with `triage_enabled` off) and `triage_changes` (an older capture it
  displaced). A caller that already processed and triaged the burst (the optional Sonnet stage)
  passes `processed=` and `triage=`.
- `process_burst(stills: Sequence[bytes], settings) -> ProcessedBurst` (`selected`, `sharpness`,
  `still`, `page`, `page_detected`, `width_px`, `height_px`, `page_corners`) is the same processing without
  storing; `transcript_window(session_t_ms, settings) -> {"t_start", "t_end"}`.
- The building blocks (`decode_image`, `sharpness`, `pick_sharpest`, `downscale`, `find_page`,
  `crop_page`, `enhance_contrast`) are importable from `studentassistant.sources.captures`.
- Everything is CPU-bound (OpenCV, `opencv-python-headless`): the capture upload
  (`server/captures.py`) runs `store_capture` in a worker thread.

### Capture triage -- `triage.py`, `triage_llm.py` (#324)
Every capture is triaged when it is stored, by cheap deterministic checks (OpenCV/NumPy, no GPU,
no LLM), so a bad one never reaches the notes. A capture that fails is **set aside**: kept in the
vault with its reason, never deleted, never transcribed, never given to the editor.

- `triage_capture(processed: ProcessedBurst, others: Sequence[TriageRecord], settings) ->
  TriageResult` (pure; `decide(metrics, others, settings)` is the same on computed metrics).
  `TriageResult`: `status` (`kept` | `flagged` | `set_aside`), `reasons` (`blank` | `duplicate` |
  `blurry` | `partial` | `same_content`), `duplicate_of` (topic-relative source id or null),
  `metrics`, `decided_by` (`auto` | `student` | `legacy`), `decided_at`, `note` and `history`
  when there are; `replaces` (not stored) names an older capture a sharper duplicate displaces.
  `TriageRecord`: `source_id`, `status`, `dhash`, `dhash256`, `sharpness`, `protected` (the
  current notes link it, or the student decided about it). The metrics
  (`capture_metrics(processed)`): `ink_ratio` (share of ink pixels of the page image, measured
  inside an 8 % margin; a pixel is ink when 25 % darker than the mean of its 41 px neighbourhood,
  after a 2x2 opening, at a 1200 px long edge -- paper texture, a shadow and a squared notebook's
  faint grid are not ink), `border_ink` (the largest ink share of the four 2 % border bands of the
  frame), `sharpness` (the kept still's Laplacian variance, as `pick_sharpest` scored it),
  `dhash` (64-bit, 16 hex digits) and `dhash256` (the finer 256-bit one) of the page image's ink
  density smoothed by a Gaussian of 2 % of its long edge (on grey levels a mostly white page's
  bits flip between two shots of one page), `page_detected`, `page_border_sides` and, when
  compared, `duplicate_distance` / `duplicate_distance_256`. The checks:
  - **blank**: `ink_ratio < triage_blank_max_ink`; a blank page is only `blank`.
  - **duplicate**: the nearest kept capture of the same topic and kind (their stored hashes) at a
    64-bit distance `<= triage_duplicate_max_distance`, confirmed by the 256-bit one within four
    times that (two different pages of lined handwriting can come within 10 bits on 64). The
    newer one is set aside as `duplicate` of it, unless it is not blurry and
    `triage_duplicate_sharper_ratio` times sharper and the older one is not protected: then it is
    kept and `store_capture` sets the older one aside (`apply_swap`, the old decision kept in
    `history`).
  - **blurry**: `sharpness < triage_min_sharpness`.
  - **partial**: a detected page with corners on two or more image sides, or `border_ink >
    triage_partial_max_border_ink`: `flagged` (kept, with the reason), `set_aside` with
    `triage_partial_sets_aside`.
- Stored in the sidecar by `store_capture` in the same `put_source` call: `triage: {status,
  reasons, duplicate_of, metrics, decided_by, decided_at}`; later changes go through
  `vault.update_page_meta`. The upload route publishes `capture.triaged` (`CAPTURE_TRIAGED_KIND`,
  `triaged_payload(change)`: `capture_id`, `source_path`, `source_id`, `status`, `reasons`,
  `duplicate_of`, `decided_by`, `capture_session_id`; `server.md`); the observer's fold ignores
  it.
- **Content duplicates**: `check_same_content(vault, s, t, source_path, text, settings) ->
  [TriageChange]`, run by the page transcriber after a page's Markdown is stored: its normalised
  text (`normalise_text`: no case, accents, Markdown, HTML comments or `[[?...]]` marks) is
  compared word by word (`difflib`) with every kept page of the topic and kind that has a
  transcription; a pair at least `triage_same_content_min_similarity` alike sets aside the one
  with more `[[?` marks (ties: the less sharp, then the newer) as `same_content`, `duplicate_of`
  the other -- never a protected page. Transcriptions under five words are not compared. Each
  change is published as `capture.triaged` (origin `sources`).
- **Student decision**: `set_capture_triage(vault, s, t, source_id, "set_aside" | "restore", *,
  reason=None, sync) -> TriageResult`: `decided_by: student`; restore is `kept` with no reasons;
  `reason` is kept in `reasons` when it is a triage reason, else as `note`; the previous decision
  goes to `history`. Commits (`sync.checkpoint`) `Página N de <s>/<t> apartada` / `recuperada`.
  The caller (the server's chat routing, #327) publishes `capture.triaged` with origin `user`.
  A restored page with no transcription is owed again: the transcriber takes a student's
  `capture.triaged` that is not `set_aside` and transcribes the page at once
  (`PageTranscriber.enqueue(session_id, source_path)`), and the catch-up finds it owed too.
- **Excluded downstream**: `PageTranscriber` checks the sidecar before transcribing and skips a
  set-aside capture (no call, no `page.transcribed`, no pending items); `catchup.owed_pages(...,
  set_aside=)` / `read_owed` leave set-aside captures out; `editor.inputs.assemble_input` leaves
  them out of the catalogue and the sources, and leaves out the open pending items whose page refs
  are all set-aside captures (flagged ones stay in).
- **Readers**: `triage_status(vault, s, t) -> {source_id: TriageResult}` for every stored source
  (topic-relative ids; a source with no `triage` block -- stored before #324, or not a capture --
  reads as `kept`, `decided_by: legacy`); `set_aside_ids(vault, s, t)`; `is_set_aside(vault,
  source_path)`; `triage_of(meta)`.
- **Optional Sonnet stage** (`triage_llm.py`, `[sources] triage_llm_check = false` by default):
  `refine_triage(client, page, result, settings)` sends the page image (role `transcriber`,
  prompt `capture_triage`, strict tool `triage_verdict` through `llm.structured`) only when a
  check is within `triage_llm_margin` (relative) of its threshold (`ambiguous_checks`: `blank`,
  `blurry`, `partial`; the duplicate check stays deterministic); the verdict replaces those
  checks (`with_verdict`, recorded as `metrics.llm_verdict` / `llm_reason`); any `LLMError` keeps
  the deterministic result. The upload route runs it before storing, with a client bound to the
  session's ledger.
- **CLI**: `studentassistant triage <subject> <topic> [--apply]` (`retro_triage`) triages every
  stored capture oldest first, as if it arrived now, and prints its metrics and decision (and the
  stored one, if any); it only reads. With `--apply` it writes the decision of each capture with
  no `triage` block (and sets aside an older duplicate it displaces) and commits (`Triaje de N
  capturas de <s>/<t>`). For calibrating the thresholds on a real vault and triaging captures
  stored before #324.

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
     `attempts`. All of them have origin `sources` (ADR-0003: what the sources module produces
     on its own, #360; logs written before carry `observer`, and the observer's readers treat
     both alike, `observer.ops.AUTOMATIC_ORIGINS`).
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

### Scanned PDF pages -- `pdf_transcription.py` (#257)
A kept page whose PyMuPDF text is empty (`has_text` false: a scan with no text layer) is
transcribed with Claude vision into `page-NNN.pKKK.md`, next to its `.txt` and `.jpg`:

- `render_pdf_page(content, page, *, long_edge, quality) -> bytes` (in `pdf.py`) renders page `K`
  as a JPEG at `[sources] pdf_transcription_long_edge` / `pdf_transcription_quality`;
  `read_pdf_page_input(vault, s, t, pdf_path, page, settings) -> PdfPageInput` adds the context
  (subject, topic, original file name, original page number).
- `await transcribe_pdf_page(client, page, vault) -> PdfPageTranscription`: one `transcriber` call
  with the `page_transcription_pdf` prompt (the printed-text variant of the page prompts: layout
  kept, `[[?word]]` / `[[?]]` marks, no spoken hints, no printed page number asked), the image
  first, then the context; the Markdown is written with `vault.put_page_transcription(vault,
  pdf_path, text, page=K)`. Raises like `transcribe_page`.
- `ScannedPdfTranscriber(settings=, client_factory=, on_write=)` runs those calls in the
  background with a `transcriber` client bound to the topic's ledger (no session). `schedule(vault,
  s, t, [(pdf_path, K), ...])` queues pages (a page already queued is not queued twice, and one
  whose `.md` is there by the time its turn comes is skipped); the web upload calls it after the
  import, never waiting for it (`app.state.pdf_transcriber`, built with the page transcriber when
  the app has an LLM transport and `transcription_enabled`). `catch_up_vault(vault)` is a
  `SessionService.add_on_open` hook: once per start it queues every page
  `scanned_pages_without_transcription(vault, s, t)` (in `pdf.py`) lists for every topic -- pages
  whose `.txt` is stored but empty and with no non-empty `.md` (a restart cut the job, a cost cap
  stopped it, or the PDF came in through the CLI). Concurrency, attempts and retry waits are the
  page transcriber's `transcription_*` settings; a reached cost cap, a refusal, an unreadable
  stored PDF or the last failed attempt is logged and leaves the page without `.md` (the next start
  tries again). Each exchange is appended to `conversations/transcriber-pdf.jsonl` (the image as
  `{"type": "vault", "path": <pdf>, "page": K}`); `on_write` tells the sync to commit. Uncertain
  words stay marked in the Markdown; they open no pending item (no session holds a PDF).
- `read_pdf_page_text(vault, pdf_path, K) -> PdfPageText | None` (in `pdf.py`) is how the editor
  reads a PDF page as text: the `.txt` when it holds any text, else the `.md`
  (`PdfPageText.transcribed`), else `None`.

### Web search -- `web.py`, `web_searcher.py` (#59)
Not re-exported by the package root (they import `studentassistant.llm`): import them from their
submodules. "Busca esto en Internet" runs Claude's server-side web tools through
`studentassistant.llm` (`web_search_tool`, `web_fetch_tool`, `run_server_tools`; the versions come
from `[llm] web_search_tool` / `web_fetch_tool`, `*_20260209` by default).

Under each backend (#306): with `api` the tools run on Anthropic's servers; with `claude-code`
the same requests run on the CLI's own `WebSearch` / `WebFetch` tools (only for the
`web_search_role` client) and come back as the same blocks, so nothing here branches on the
backend -- the fetched text is then the CLI's rendering of the page as Markdown (see
`docs/modules/llm.md`). When the backend cannot run them (the CLI tool turned off in
`[llm.claude_code]` or missing, or a fetch that brings nothing back), `search_web` raises
`WebSearchError` and `snapshot_page` `WebFetchError` with `WEB_UNAVAILABLE_MESSAGE` ("La búsqueda
web no está disponible con el backend de Claude Code..."): a search is recorded `search.failed`
(`reason: error`) with it, a keep answers 422 with it; nothing is retried or written.

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
  `search_id`, `summary`, `session`, `added_via` (`url` | `share`, a page given by its address,
  #62). A page that looks like it carries a key: `SecretRefused`,
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
  `web.search_failed` (`search_id`, `query`, `reason`, `message`) on it, origin `sources`. With
  `web_auto_keep` the `relevant` results are kept at once (`kept_by: assistant`).
  `keep(vault, s, t, search_id, index, *, kept_by="student", session_id=None) -> KeptWebSource`
  fetches and stores one result, records `search.kept`, calls `on_write` and publishes
  `web.snapshot_stored` (`search_id`, `index`, `url`, `source_id`, `title`, `kept_by`) on the live
  session; keeping a result again returns the first snapshot. `UnknownSearchError` (404),
  `SearchNotDoneError` (409) and `KeepError` (422: not a text page, or a key) carry Spanish
  messages. `list(vault, s, t)` is `list_web_searches` with a `queued` search no job runs shown as
  failed, `reason: interrupted` (the server stopped). `stop()` cancels what is running;
  `wait_idle()` waits for every queued search.
- `keep_url(vault, s, t, url, *, added_via="url", kept_by="student", session_id=None) ->
  (KeptWebSource, already_kept)` (#62) keeps a page the student gave by its address (pasted in the
  web UI: `url`; shared to the phone: `share`) the same way: `snapshot_page` with a client bound
  to the topic's ledger and `session_id` (the topic's live session, if any), `keep_snapshot`
  with `added_via` in the sidecar (no `search_id`/`query`/`summary`), `on_write`, and
  `web.snapshot_stored` (`url`, `source_id`, `title`, `kept_by`, `added_via`) on the live session.
  No search record is written. An address the topic already has (`web.find_kept_url(vault, s, t,
  url)`: a web sidecar whose `url` or `requested_url` is it, after trimming) is returned with
  `already_kept` true and nothing fetched. `NotAWebPageError` (a `KeepError`, 422) for a
  non-http(s) address; the other refusals as `keep`.
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
| `max_pasted_image_bytes` | 10 MiB | an image pasted into the notes (`POST .../sources/images`, server) is refused with 413 |
| `pdf_thumbnail_long_edge` | 1200 px | -- |
| `pdf_thumbnail_quality` | 85 | -- |
| `pdf_transcription_long_edge` | 1568 px | a scanned PDF page is rendered at it for its vision transcription (Claude downscales larger images) |
| `pdf_transcription_quality` | 90 | -- |
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
| `triage_enabled` | true | false: no capture is triaged (nothing is set aside) |
| `triage_blank_max_ink` | 0.002 | a page image with less ink (share of pixels) is `blank` |
| `triage_duplicate_max_distance` | 10 | a capture within this many dHash bits (of 64) of a kept one of the topic is a `duplicate` |
| `triage_duplicate_sharper_ratio` | 1.25 | a duplicate this much sharper replaces the older capture instead |
| `triage_min_sharpness` | 25 | a still with a lower Laplacian variance is `blurry` |
| `triage_partial_max_border_ink` | 0.01 | more ink along one side of the frame is `partial` |
| `triage_partial_sets_aside` | false | true: a `partial` capture is set aside, not only flagged |
| `triage_same_content_min_similarity` | 0.85 | two transcriptions this alike set the worse page aside |
| `triage_llm_check` | false | true: checks near a threshold are asked to Claude (`transcriber` role) |
| `triage_llm_margin` | 0.25 | how near (relative to the threshold) counts as ambiguous |
