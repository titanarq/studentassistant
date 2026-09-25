# Module: server

**Lives in:** `backend/src/studentassistant/server/` and the CLI entry points in
`studentassistant/cli.py`.

## Responsibility
- FastAPI app factory, LAN bind, pairing + bearer auth (ADR-0001), static web assets.
- Session lifecycle (create/resume/end) and the in-process **session event bus** that feature
  modules (stt, sources, observer, editor) subscribe to.
- WebSocket gateway: audio frames -> stt pipeline, client events -> bus, bus -> phone.
- Capture upload endpoint -> `sources`.
- REST (+ SSE for streaming) for the web UI: topics, notes, sources, pending, editor chat,
  generators.
- `studentassistant replay <dir>`: feeds a recorded session (WAV + timed captures + events)
  through the same pipeline as a live phone; `serve --record` saves raw inputs for replay under
  `~/.cache/studentassistant/recordings/` (never the vault).

## Boundaries
- Thin: HTTP/WS concerns only; logic lives in feature modules.

## Public surface

### `create_app(static_dir=None, *, server=None, codes=None, vault=None, sync=None, vault_settings=None, stt=None, sources=None, recorder=None, llm_transport=None, llm_settings=None) -> FastAPI`
`studentassistant.server.app.create_app` builds a fresh app (one per caller; nothing is registered
at import time). `server` is the `[server]` config section (`ServerSettings`; default: read from
`studentassistant.config`), `codes` the in-memory `PairingCodes` (tests inject one with a fake
clock). `vault` is the open `Vault` the session routes work on (tests pass `tmp_vault`); without
one, the vault at `vault_settings.path` (default: the configured `[vault]` section) is opened on
the first request that needs it, so building an app never touches a vault. `sync` is the vault's
`GitSync` (default: one over that vault with `vault_settings.git`). `stt` is the `[stt]` section
(`SttSettings`, default: the configured one) the session WebSocket follows; `sources` the
`[sources]` section (`SourcesSettings`, default: the configured one) the PDF upload follows.
`recorder` is the `SessionRecorder` of `serve --record` (see "Recording and replay" below);
without one nothing is recorded, and one whose directory is inside the vault is refused with
`ValueError`. `llm_transport` turns the live observer on (`observer.live.ObserverLoop`,
`docs/modules/observer.md`): with one and `[observer] enabled` in `llm_settings` (a full
`Settings`, default: the configured one; it also gives the observer role, caps and prices), the
lifespan starts and stops the loop and its `flush` is an `add_before_ended` hook. `serve` passes
the real Anthropic transport; without one (every other caller, tests included) no Claude call is
made.

The app's lifespan drives that `GitSync`: on startup it calls `SessionService.startup()`, so once
the vault is open (still lazily, on the first request that needs it) `GitSync.run()` runs as a
background asyncio task and commits and pushes batched changes on its own schedule; on shutdown
`SessionService.shutdown()` cancels that task and runs `GitSync.flush()` in a worker thread, so no
noted change is left uncommitted. Without the lifespan (a `TestClient` used outside `with`) there
is no background loop.

`app.state` holds `server`, `codes`, `devices` (the `DeviceStore`), `bus` (the app-wide
`SessionBus`), `sessions` (the `SessionService` over the vault, whose `bus` is `app.state.bus`)
`gateway` (the `SessionGateway` of the session WebSocket), `recorder` (`None` unless one was
given), `observer` (the `ObserverLoop`, `None` without an `llm_transport`) and `notes` (the
`NotesGenerator` of the notes generation route, `None` without an `llm_transport`).
Routes registered today:

- `GET /api/health` -> protocol v1 `rest.health.response`, built with the backend protocol model:
  `{"status": "ok", "protocol_version": "<PROTOCOL_VERSION>", "server_time_ms": <epoch ms>}` and no
  other key (strict clients reject unknown keys), whether or not the web app is built.
  Unauthenticated, still behind the LAN guard and the Host allowlist. The package version is not
  part of it; it stays in the app's OpenAPI metadata (`GET /openapi.json` `info.version`, behind the
  usual bearer check).
- `POST /api/pair/codes` (loopback callers only; anyone else gets 403) -> `{"url", "code",
  "expires_at"}`: a one-time pairing code (`XXXX-XXXX`, from an alphabet without 0/O/1/I) valid
  for 5 minutes and redeemable once. `url` is the LAN base URL for the capture client:
  `server.public_url` when set, else `http://<this PC's LAN address>:<server.port>`. A local
  administrative endpoint, not part of protocol v1: `studentassistant pair` and the web UI's
  `/pair` page call it.
- `POST /api/pair` (`rest.pair.request` -> `rest.pair.response`): redeems a code and returns a new
  `device_id` and bearer `token`. An unknown, expired or already redeemed code is 401 with the same
  body in all three cases. Codes live only in the serving process's memory.
- Subjects, topics and the session lifecycle (`server/session_routes.py`), protocol v1 bodies in
  and out, optional fields left out rather than `null`. Every one needs the bearer token (none is
  in `EXEMPT_ROUTES`). Ids are vault slugs: `subject_id` is the subject's slug, `topic_id` the
  topic's slug within its subject, `session_id` the vault's `YYYYMMDD-HHMMSS` id; a path id
  outside the protocol's id pattern is 422.
  - `GET /api/subjects` -> `rest.subjects.list.response`; `POST /api/subjects`
    (`rest.subjects.create.request`) -> 201 `rest.subjects.create.response`.
  - `GET /api/subjects/{subject_id}/topics` -> `rest.topics.list.response`, each topic with
    `open_session_id` when it has an unended session and, for a caller speaking 1.1+ (the
    principal's `protocol_version`), `last_session_at_ms` (its latest study session's start, from
    `vault.list_sessions`; review sessions, `kind: review`, are left out, #191) when it has any
    and `pending_count` (open items of
    `observer.load_observer_snapshot(..., write_back=False)`, so listing writes nothing); either
    is left out, with a warning logged, when the vault or observer state cannot be read, and
    both are always left out for a device that paired as a 1.0 client. For a caller speaking
    1.3+ a topic also carries `digest_excerpt`
    (`observer.digest_excerpt(observer.topic_digest(...))`, cut to `protocol.DIGEST_EXCERPT_MAX`)
    once a session of it has ended; an unreadable digest is logged and left out (#192). `POST /api/subjects/{subject_id}/topics`
    (`rest.topics.create.request`) -> 201 `rest.topics.create.response`.
  - `POST /api/sessions` (`rest.sessions.start.request`) -> 201 `rest.sessions.start.response`;
    `POST /api/sessions/{id}/resume` (no body) -> `rest.sessions.resume.response`;
    `POST /api/sessions/{id}/end` (`rest.sessions.end.request`) -> `rest.sessions.end.response`.
    `received_capture_ids` lists the captures already stored for the session (the `capture_id`s
    of its `capture.stored` events, in log order; see the capture upload below), so a resuming
    client re-uploads only the rest.
  - Errors, as `{"detail": "...", "code"?: "..."}` (see "Error bodies" below): an unknown
    subject, topic or session is 404; another session active or unended (start, resume) is 409
    `session_open` with its id in `X-Open-Session-Id`; resuming or
    ending an ended session is 409; a start refused because pulling the vault hit a conflict is
    409 with the conflicting paths in `detail`; a vault that cannot be opened is 503.
- `POST /api/sessions/{id}/captures` (`server/captures.py`, `captures_router()`): one burst of
  stills, protocol v1 "Capture upload (idempotent burst)". Needs the bearer token like every
  non-exempt route; every refusal is `{"detail": "..."}` with a Spanish `detail` and stores and
  publishes nothing.
  - Body: `multipart/form-data`, a `metadata` part holding the `rest.sessions.captures.request`
    JSON (validated with `protocol.CaptureUploadRequest`) plus one part per image, named by
    `images[].part`, whose `Content-Type` must equal the image's declared `content_type`. The
    body is parsed as it streams in (python-multipart), never buffered whole or spooled to disk:
    what it holds is bounded by the two limits below plus 64 KiB of metadata.
  - 422: not multipart, a malformed or truncated body, `metadata` missing or invalid (no field
    beyond the protocol's; `trigger` is `button | command`; `command_id` present exactly when
    `trigger` is `command`), a part named twice, a named image absent or empty, a part
    `metadata` does not name, or a `Content-Type` that differs from the declared one.
  - 413: an image part over `server.max_capture_image_bytes`, more image parts (or declared
    images) than `server.max_capture_images`, or a `Content-Length` beyond what those limits
    allow; the read stops at the first byte over.
  - The session must be the active one (`SessionService.require_active`): unknown 404, ended or
    unended-but-not-resumed 409, a vault that cannot be opened 503.
  - Source context: a capture is stored under the session's current source context,
    `captures.current_source_context(session)`: the `source` of the session's latest persisted
    `button` event whose `button` is `switch_source` (as the WebSocket gateway publishes it),
    mapped `notes` -> `notes`, `book` -> `book`, `pdf` -> `pdf`; `notes`
    (`DEFAULT_SOURCE_KIND`) when there is none. It is read from `events.jsonl` on every upload
    (with the stored captures, in one worker thread, under the per-session lock), so it survives
    a backend restart.
  - Stored: the burst goes to `sources.store_capture` (in a worker thread, followed by
    `GitSync.note_change()`), under `sources/<source context>/`: the sharpest still downscaled
    as `page-NNN.jpg`, its cropped page `page-NNN.page.jpg`, every other still as it came as
    `page-NNN.burst<K>.<ext>` (see `docs/modules/sources.md`). A burst none of whose images
    decodes is 422, storing nothing. The sidecar `meta` the route gives: `capture_id`, `session`,
    `captured_at` (`images[0].client_time_ms` as ISO 8601 UTC), `trigger`, `command_id` (when
    present), `image_count`, `source_context`; `store_capture` adds the stored still's
    `width_px`/`height_px`, `session_t_ms`, `transcript_window` and the processing record. The
    capture's session time is the burst's `client_time_ms` plus the clock offset of the session's
    latest WebSocket `hello` (`SessionGateway.clock_offset_ms(session_id)`, `None` -> 0 when no
    socket said hello) minus the session's `started_at_ms`, never below 0. Then the persisted bus
    event `capture.stored` (origin `phone`; `observer.CAPTURE_EVENT_KIND`, which the observer's
    fold registers) is published with payload `capture_id`, `trigger`, `command_id` (when
    present), `image_count`, `client_time_ms`, `source_path` (the stored still, relative to the
    vault root), `page_path` (the page image, likewise) and `source_context` (the same value as
    the sidecar's). The WebSocket gateway acknowledges that event to the connected
    client (see "Forwarded to the client" below). Answer: 201 `rest.sessions.captures.response`,
    `status: "stored"`, `image_count` the images in the burst, `received_at_ms` the backend
    clock.
  - Idempotent on `capture_id`: the stored ids of a session are its `capture.stored` events
    (`sessions.stored_captures(session)`, reading `events.jsonl`, so it survives a restart). A
    stored id is answered 200 `status: "duplicate"` with the stored `image_count`, storing and
    publishing nothing. The check and the store are serialised per session, so concurrent
    uploads of one new id store it once. A session that ends between the check and the event is
    409 (the source file may stay; the observer never sees it without its event).
- `GET /api/vault/status` (`server/vault_status.py`, web-only, not phone protocol) -> the vault's
  sync state without blocking anything: `host`, `pending_changes`, `pending_commits`,
  `last_commit_at`, `last_push_at`, `last_push_failure` (`kind`, `message`, `at`), `last_sync`
  (`outcome`, `message`, `conflicts`, `at`), `host_warning` (another PC's open claim on the vault
  as of the last pull: `host`, `session_id`, `subject`, `topic`, `claimed_at`, Spanish
  `message`) and `divergence` (`paths`, `local_commit`, `remote_commit`, `detected_at`, Spanish
  `message`), null when absent. It opens the vault (pull + active-host check) if nothing had.
  `GET /api/vault/divergence?path=` -> `{path, local, remote}`, both sides' text of one diverging
  path; 404 for a path not diverging. A vault that cannot be opened is 503.
- Session start/end and the active host (ADR-0002, `vault/active.py`): after the start's pull,
  `SessionService` checks `.sa/active.yaml` into `host_warning` (logged, never refused), claims it
  for its host once the session exists, checkpoints (`sesión <id> iniciada en <host>`) and calls
  `GitSync.request_push()`; the end releases the claim before its checkpoint and push.
- `GET /api/cost` (`server/cost.py`) -> the `CostStatus` of `studentassistant.llm.cost_status`
  as JSON: `session_usd`, `day_usd`, `max_usd_per_session`, `max_usd_per_day` (null = no cap),
  `observer_paused`, `editor_needs_confirmation`, plus `unpriced_session_calls`,
  `unpriced_day_calls` and `unpriced_models` (calls of a model missing from `[llm.prices]`, which
  add 0 to the totals). Optional query parameters `subject` and `topic` (slugs, given together) and
  `session` select the session whose total is reported; without them `session_usd` is 0 and only
  the day total drives the flags. A `session` without `subject` and `topic`, or only one of those
  two, is 422, an unknown subject/topic 404 (`"No existe ese tema en la bóveda."`), a vault that
  cannot be opened 503; every `detail` is Spanish. The vault and the caps come from `studentassistant.config`, read on every
  request. The server computes no cost itself. Needs the bearer check like every non-exempt route.
  A local endpoint, not part of protocol v1.
- **The web's read API** (`server/read_routes.py`, `read_router()`), read-only, over the vault the
  session service opened (`SessionService.open_vault()`) and only through the vault's public
  readers (ADR-0002), each call in a worker thread. Every route needs the bearer check (none is in
  `EXEMPT_ROUTES`); path ids and the `subject`/`topic` query ids follow the protocol's id pattern
  (422 otherwise); a vault that cannot be opened is 503 (`"No se puede abrir la bóveda."`); an
  unknown subject or topic is 404 (`"No existe ese tema en la bóveda."`). Bodies are Pydantic
  models, so they appear in `GET /openapi.json`.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/summary` -> `TopicSummary`: `sources`
    (counts of `notes`, `book`, `pdf`, `web` from `list_sources`), `sessions` (the number of study sessions;
    review sessions, `kind: review`, are left out, #191) and `session_minutes` (ended ones by `ended_at - started_at`, an unended one up to now; minutes
    rounded to 0.1), `open_pending` (`len(open_pending())` of the observer's
    `load_observer_snapshot`, which may write the refreshed snapshot back), `notes_version` (the
    highest `version` of `GitSync.list_notes_tags(subject_id, topic_id)`, `null` without tags)
    and `generated` (`list_generated`: vault-relative paths; empty until a generator exists),
    `digest_excerpt` (the topic digest's summary paragraph, `observer.digest_excerpt`, `null`
    before the topic's first session end).
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/notes` -> `TopicNotes` (`subject_id`,
    `topic_id`, `text` of `notes/apuntes.md`, `version` as above); notes not written yet are 404
    (`"Todavía no hay apuntes de este tema."`).
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/pending?status=all|open|closed` ->
    `TopicPending`: `open_count` (of the whole queue) and `items`, the observer's `PendingItem`s
    (`id`, `kind`, `text`, `refs` {`pages`, `segments`, `sources`}, `created_by`, `status`,
    `resolution`, `added_at`, `resolved_at`, `merged_ids`), open first, filtered by `status`
    (default `all`; `closed` = resolved, auto-resolved or dismissed). Read from the fold
    (`load_observer_snapshot(write_back=False)`), so it writes nothing.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/digest` -> `TopicDigest`: `text` (the
    stored `state/digest.md`, `observer.topic_digest`; `null` before the first session end) and
    `excerpt` (its summary paragraph). Reads the file only, writes nothing (#56).
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/sessions` -> `TopicSessions`: `sessions`, by
    id, each `session_id`, `started_at`, `ended_at` (`null` while unended), `minutes`, `kind` (`study` or `review`) and
    `label` («Sesión de estudio» or «Revisión de dudas»).
  - `GET /api/sessions/{session_id}/transcript?subject=..&topic=..&t=HH:MM:SS-HH:MM:SS` ->
    `TranscriptSpan`: `ref` (`sessions/<id>#t=<span>`, ADR-0005's provenance form), `start_ms`,
    `end_ms` and the `segments` (`seq`, `t_start`, `t_end` in session milliseconds, `text`) that
    overlap the span (share some time with it; a point span `T-T` takes the segments spanning
    `T`). A malformed or reversed span is 422 with a Spanish `detail`; a session the topic does not
    list is 404 (`"No existe esa sesión en ese tema."`). Open sessions are readable too.
  - `GET /api/sources/{vault_id:path}` -> the bytes of a source, `vault_id` being its
    vault-relative path (`subjects/<s>/topics/<t>/sources/<kind>/<file>`, what `list_sources`
    gives). Served with `X-Content-Type-Options: nosniff` and a `default-src 'none'; sandbox`
    CSP; images (`jpeg`, `png`, `webp`, `gif`, `heic`/`heif`), `application/pdf`, `text/markdown`
    and `text/plain` keep their media type (text with `charset=utf-8`), any other `text/*` goes out
    as `text/plain` and everything else (HTML, SVG, YAML, unknown) as `application/octet-stream`,
    so no source is ever rendered as a page of this origin.
  - `GET /api/sources/{vault_id:path}/meta` -> `SourceMeta`: `vault_id`, `kind`, `media_type` (the
    one the content route serves), `size`, `meta` (the parsed sidecar, `null` without one) and
    `transcription` (the sidecar's `transcription` when it is text, else `null`).
  - Both source routes go only through the vault's `read_source`: a path that is not a topic's
    `sources/<kind>/<file>` (absolute, `..` or `%2e%2e`, backslash, NUL, a symlink out of its
    directory) or names no file is 404 (`"No existe esa fuente en la bóveda."`); nothing outside
    the vault's sources is ever served.
- `GET`/`PUT /api/subjects/{subject_id}/topics/{topic_id}/book` (`server/book_routes.py`,
  `book_router()`, #58): the topic's textbook title (`vault.get_book`/`set_book`). `GET` answers
  `BookResponse` (`subject_id`, `topic_id`, `title`, `null` when none was set); `PUT
  {"title": "..."}` (at most 200 characters) records it and calls `SessionService.note_change()`.
  Unknown subject or topic 404 (`"No existe ese tema en la bóveda."`), empty title or one that
  looks like a key 422, an unopenable vault 503. No session needed.
- `POST /api/subjects/{subject_id}/topics/{topic_id}/sources/pdf` (`server/pdf_upload.py`,
  `pdf_upload_router()`): the web's PDF import, what `studentassistant import-pdf` does from the
  CLI. Body: `multipart/form-data` with one `file` part (the PDF; its `filename` names it,
  `documento.pdf` without one) and an optional `pages` part, the range as typed (`82-94`,
  `páginas 82 a 94`; empty or absent keeps every page, parsed by `sources.parse_page_range`).
  Parsed as it streams in: a `file` over `[sources] max_pdf_bytes` (or a declared/received body
  over it plus 64 KiB) stops the read with 413. Then `sources.import_pdf` runs in a worker thread,
  one import per topic at a time (so two never race for the next `page-NNN`), and
  `SessionService.note_change()` lets the sync loop commit and push it. Answer: 201
  `PdfImportResponse` (`subject_id`, `topic_id`, `source_id`, `vault_id`, `original_name`,
  `original_page_count`, `first_page`, `last_page`, `page_count`, `pages_without_text` in the
  original's numbering). Refusals keep `import_pdf`'s Spanish message: `PdfTooLargeError` (file,
  range page count, kept pages) 413; `PdfUnreadableError`, `PageRangeError`, a PDF that looks like
  a key, a body that is not multipart, a missing/empty/repeated `file`, or any other part 422; an
  unknown subject or topic 404 (checked before the body is read); a vault that cannot be opened
  503. No active session is needed. Needs the bearer check like every non-exempt route.
- `GET /api/search?q=..&subject=..&topic=..&kinds=..&limit=..` (`server/search_routes.py`,
  `search_router()`) -> protocol `rest.search.response` (`query`, `hits`), through the
  `VaultIndex` the session service opened (`SessionService.index`), `VaultIndex.search` in a
  worker thread; optional hit fields are left out, never `null`. `q` is plain text (at most 500
  characters; every word must appear, as a prefix, accents and case ignored; no word, no hits).
  `subject` and `topic` filter by slug (protocol id pattern; `topic` needs `subject`, else 422);
  an unknown one matches nothing. `kinds` is a comma-separated subset of `notes`, `page`, `pdf`,
  `web`, `transcript` (default all; an unknown kind is 422 naming it). `limit` 1-100, default 20.
  Each hit: `kind`, `path` (the vault-relative file), `source` (the source it belongs to, for
  `GET /api/sources/{source}`; absent for notes and transcripts), `subject`, `topic`, and for a
  transcript `session`, `seq` and `t_start` (session ms); `snippet` marks each matched term
  between `\x02` and `\x03`. A vault that cannot be opened is 503 (`"No se puede abrir la
  bóveda."`), an index that could not be opened 503 (`"El índice de búsqueda no está
  disponible."`). Needs the bearer check like every non-exempt route.
- `POST /api/subjects/{subject_id}/topics/{topic_id}/notes/generate` (`server/notes_routes.py`,
  `notes_router()`): "prepárame el tema", web-only, not phone protocol. Optional JSON body
  `{"confirm_over_cap": false}`. Opens the vault through the `SessionService`, builds
  `get_client("editor", ...)` over the app's `llm_transport` and `llm_settings` bound to the
  topic's ledger (`LedgerBinding(vault, subject, topic)`), and awaits
  `editor.generate.generate_notes` with the session service's `GitSync`; answers 200 with its
  `GenerationResult` (`docs/modules/editor.md`: valid notes committed and tagged
  `<subject>/<topic>/apuntes-vN`, or a draft with `warning` and `errors`). The `notes.generated`
  event is published on the bus (persisted, origin `editor`) when the topic's session is the
  active one (else it is only in `conversations/editor.jsonl`). One generation per topic at a
  time. Errors, Spanish `detail`: no `llm_transport` 503 (`"La generación de apuntes no está
  disponible: ..."`), an unknown topic 404, a generation of the topic already running 409, a
  reached cost cap 409 `cost_cap_reached` (`"Se ha alcanzado el límite de gasto ... Confirma
  ..."`) until the body
  says `confirm_over_cap`, a Claude refusal or failure 502, a vault that cannot be opened 503.
  Needs the bearer check like every non-exempt route.
- **The doubts API** (`server/doubts_routes.py`, `doubts_router()`), web-only, not phone
  protocol, for the pending panel (#80): thin over `editor.doubts` (`docs/modules/editor.md`),
  over the vault and `GitSync` of the `SessionService`, host `SessionService.host` for the review
  sessions. One doubts operation per topic at a time; reviewing and answering also hold the
  topic's notes lock with "prepárame el tema" (`NotesGenerator.claim`) and call the `editor` role
  through `llm_transport`, bound to the topic's ledger.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/doubts` -> `DoubtsQueue`: `subject`,
    `topic`, `open_count`, `current` (the id of the open doubt to ask next, `null` when none) and
    `items`, open first, each `{item, question, outcome}`: `item` is the observer's `PendingItem`
    (as in `/pending`), `question` `null` or `{pending_id, question, suggestions, options:
    [{source_id, says}], asked_at}`, `outcome` `null` or `{pending_id, status, resolution,
    evidence: [{source_id, quote}], answer, suggestion, chosen_source, discarded, keep_discarded,
    notes_changed, warning, resolved_at}`. Reads only.
  - `POST .../doubts/review`, optional body `{"confirm_over_cap": false}` -> `ReviewResult`
    (`auto_resolved`, `asked`, `notes_changed`, `session_id`, `commit`, `attempts`, `warning`,
    `model`): the editor auto-resolves what the sources answer (cited) and writes a question for
    every other open doubt. The web calls it after "prepárame el tema".
  - `POST .../doubts/{pending_id}/answer`, body `{"suggestion": 1}` (1-based) or
    `{"answer": "..."}` or, for a contradiction, `{"source_id": "sources/notes/page-001.jpg",
    "keep_discarded": true}` (an `answer` may go with either), plus `confirm_over_cap` ->
    `ResolutionResult` (`subject`, `topic`, `pending_id`, `status` `resolved`, `resolution`,
    `notes_changed`, `session_id`, `commit`, `attempts`, `warning`, `model`).
  - `POST .../doubts/{pending_id}/dismiss`, no body -> `ResolutionResult` with `status`
    `dismissed`; never calls Claude, so it works without `llm_transport`.
  - Errors, Spanish `detail` plus `code` where noted: an unknown topic or doubt 404 (`"No existe
    esa duda en este tema."`); a doubt already closed (`doubt_closed`, `"Esa duda ya está
    cerrada."`), a topic with an unended session (`session_open`, `"Este tema tiene una sesión
    sin terminar: ..."`), a review before the notes exist, another doubts or notes operation of
    the topic running, or a reached cost cap (`cost_cap_reached`) until the body says
    `confirm_over_cap` 409; an answer that does not fit the question 422; no
    `llm_transport` 503 for review and answer; a Claude refusal or failure 502; a vault that
    cannot be opened 503. Needs the bearer check like every non-exempt route.
- **The editor chat API** (`server/revise_routes.py`, `revise_router()`), web-only, not phone
  protocol, for the chat beside the notes (the web client is #71): thin over `editor.revise`
  (`docs/modules/editor.md`), over the vault and `GitSync` of the `SessionService`. A turn and an
  undo hold the topic's notes lock with "prepárame el tema" and the doubts
  (`NotesGenerator.claim`); a turn calls the `editor` role through `llm_transport`, bound to the
  topic's ledger. `notes.edited` / `notes.undone` are published on the bus (persisted, origin
  `editor`) when the topic's session is the active one.
  - `POST /api/subjects/{subject_id}/topics/{topic_id}/notes/chat`, body `{"message": "Esto está
    demasiado resumido", "confirm_over_cap": false}` (`message` 1-4000 characters) -> a
    Server-Sent Events stream (`text/event-stream`, `Cache-Control: no-cache`), each event
    `event: <name>` plus one line of JSON `data:`:
    - `reply.delta` `{"text": "...", "attempt": 1}`: the editor's reply as it is written;
    - `reply.restart` `{"attempt": 2}`: the change was sent back to the editor; drop the reply
      streamed so far, a new one follows;
    - then exactly one of `result` -- the `RevisionResult`: `reply` (authoritative; replaces the
      streamed text), `applied`, `summary`, `changed_sections`, `diff` (unified diff of
      `apuntes.md`), `notes` (the new text when changed), `fidelity_mode`, `style_rules`,
      `proposed_style_rules` (to confirm with the style guide API below), `commit`, `warning`, `errors`, ... -- or `error` `{"status": 409|502|500, "detail":
      "...", "code"?: "..."}` (a reached cost cap 409 `cost_cap_reached`, built with
      `cost_cap_error`, until the body says `confirm_over_cap`; a Claude refusal or failure 502);
      `code` is gated like a REST error body (`speaks_error_codes`); the stream then ends.
    The turn runs in its own task: a client that disconnects does not cut the change in half.
  - `POST .../notes/why` (#69), body `{"section": "causas", "block": 2, "quote": "Disponibilidad
    de carbón…", "confirm_over_cap": false}` ("¿Por qué pusiste esto?" on one block: `section` the
    anchor, `null` before the first section; `block` its number there, from 1, as the validator
    counts; `quote` the text the student sees, up to 2000 characters, which finds the block when
    the number does not match -- at least one of `block`/`quote`) -> the same stream as a chat
    turn: `reply.delta`, then `result` -- the `ExplanationResult` of `editor.explain`: `question`,
    `reply`, `refs` (`{label, kind, text, source_id, path}` per footnote the block cites, for the
    sources panel), `section`, `block`, `block_text`, `images`, `omitted`, `warning`, `model` --
    or `error` as above. Nothing of the notes changes; the answer is appended to the editor
    conversation. Holds the notes lock like a turn. A block that is not in the notes, or a
    title/rule, is 422 before the stream.
  - `GET .../notes/chat` -> `ChatHistory` (`turns`: `{time, kind, message, reply, applied,
    summary, changed_sections, commit, undone, warning, refs, proposed_style_rules}`, oldest
    first -- `kind` `explain` for a "¿Por qué?" answer, with its `refs`; `can_undo`). Reads only; works without
    `llm_transport`.
  - `POST .../notes/chat/undo`, no body -> `UndoResult` (`undone_commit`, `summary`, `commit`,
    `notes_changed`, `diff`, `notes`, `paths`): reverts the latest applied turn not yet undone
    (again for the one before). No Claude call.
  - Errors before the stream, Spanish `detail`: no `llm_transport` 503 (chat), a vault that cannot
    be opened 503, an unknown topic 404, no notes yet 409 (`"Todavía no hay apuntes de este tema:
    ..."`), another notes or doubts operation of the topic running 409, an invalid body 422; undo:
    nothing to undo or a file changed after that turn 409. Needs the bearer check like every
    non-exempt route.
- **Notes versions** (`server/versions_routes.py`): thin over `editor.versions`
  (`docs/modules/editor.md`), over the vault and `GitSync` of the `SessionService`. No Claude
  call: every route works without `llm_transport`.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/notes/versions` -> `NotesVersions`
    (`versions`: `{version, tag, commit, tagged_at, message, current}` oldest first; `has_notes`,
    `changed_since_latest`).
  - `GET .../notes/versions/diff?from=1&to=2` -> `VersionDiff` (`sections` by anchor: `key`,
    `anchor`, `title_before`, `title_after`, `level`, `status`
    `added|removed|changed|unchanged`, `moved`, `renamed`, `diff`; `footnotes`
    `{added, removed, changed}` labels; the whole `diff`; `identical`). Without `to`, against the
    current `apuntes.md` (`to_version: null`).
  - `GET .../notes/versions/{version}` -> `VersionText` (`text` as tagged).
  - `POST .../notes/versions/{version}/restore`, no body -> `RestoreResult` (`restored_version`,
    `version` and `tag` of the new `apuntes-vN`, `commit`, `diff`, `notes`, `errors`, `warning`).
    Holds the topic's notes lock (`NotesGenerator.claim`, when the app has one); `notes.restored`
    is published on the bus (origin `editor`) when the topic's session is the active one.
  - Errors, Spanish `detail`: a vault that cannot be opened 503, an unknown topic or version 404,
    a version below 1 or a diff without `from` 422, no current notes to diff with, the current
    notes already being that version, or another notes operation of the topic running 409.
- **The subject style guide API** (`server/style_guide_routes.py`, `style_guide_router()`),
  web-only, thin over `editor.style_guide` (`docs/modules/editor.md`), over the vault and
  `GitSync` of the `SessionService`; no Claude call. Each answers a `StyleGuide` (`subject`,
  `rules`, `added`, `commit`):
  - `GET /api/subjects/{subject_id}/style-guide`: the rules;
  - `POST .../style-guide/rules`, body `{"rules": ["Usa tablas para comparar conceptos."]}`: the
    student confirms rules the editor proposed (`proposed_style_rules` of a chat turn); new ones
    appended and committed, `added` lists them;
  - `PUT .../style-guide`, body `{"rules": [...]}`: the whole list, edited or with rules removed
    (`[]` clears it).
  - Errors, Spanish `detail`: an unknown subject 404, an empty or too long rule or more than 50
    422, a vault that cannot be opened 503. Needs the bearer check like every non-exempt route.
- **Error bodies** (`server/errors.py`, protocol 1.2, `protocol/README.md` "REST errors"): every
  REST error is `{"detail": "<Spanish>"}`; the refusals a client branches on also carry `code`
  (`studentassistant.protocol.ErrorCode`: `cost_cap_reached`, `doubt_closed`, `session_open`).
  A route raises `ApiError(status, detail, code, headers=None)` (an `HTTPException`) or
  `cost_cap_error(error, then)` (409 `cost_cap_reached` from a `CostConfirmationRequiredError`;
  `then` ends the Spanish sentence, e.g. `"Confirma para continuar igualmente."`); the handler
  `install_error_handler(app)` puts in `create_app` answers it, adding `code` only when
  `speaks_error_codes(principal.protocol_version)` (negotiated version >= 1.2; the PC itself
  always), so a device paired as 1.0/1.1 keeps the plain body. Every new coded refusal (e.g. the
  editor's revision routes) adds its code to `ErrorCode` and the protocol README and raises
  `ApiError`; uncoded errors stay plain `HTTPException`s.
- `WS /ws/sessions/{session_id}` (`server/ws.py`): the capture client's session WebSocket,
  described in its own section below.
- **The built web app at `/`.** `static_dir` defaults to `STATIC_DIR`, the package-relative
  `backend/src/studentassistant/server/static/` (`Path(__file__).parent / "static"` -- a
  code-layout constant, not a configuration knob; tests pass a temporary directory). The web
  build (`cd web && npm run build`, see `docs/modules/web.md`) writes there; the directory is
  git-ignored.
  - **Built** (the directory holds an `index.html`, checked once when the app is created): `GET /`
    returns `index.html` and every file under the directory is served at its relative path
    (e.g. `GET /assets/app.js`). Paths resolving outside the directory are never served.
  - **SPA fallback:** a `GET` for any other path returns `index.html` with status 200 so the web
    app's client-side router resolves it -- except paths whose first segment is `api` or `ws`,
    which stay backend-owned and answer 404, never `index.html`.
  - **Not built:** the app still starts, and `GET /` answers 200 with JSON
    `status: "ok"` and a `hint` string telling to run `cd web && npm run build` (`NOT_BUILT_HINT`).
  - The web routes are registered after every API/WebSocket route, so those keep priority. New
    backend routes must live under `/api` or `/ws`.

### Pairing and authentication (ADR-0001)

Every request passes three ASGI middlewares, in this order:

1. **LAN guard** (`server/network.py`, `LanGuardMiddleware`): the socket peer address must be
   loopback or private -- `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
   `169.254.0.0/16`, `::1`, `fe80::/10`, `fc00::/7` (IPv4-mapped IPv6 is unwrapped). Anything else
   gets 403 (HTTP) or a 1008 close (WebSocket). `X-Forwarded-For` is never trusted: `serve` runs
   uvicorn with `proxy_headers=False`, so the socket peer is the only client address.
2. **Host allowlist** (`server/network.py`, `HostAllowlistMiddleware`), against DNS rebinding (a
   page on `evil.example` re-pointed at `127.0.0.1` would otherwise count as a trusted loopback
   client). The `Host` header, port stripped and compared case-insensitively, must be
   `localhost`, a loopback or private IP literal (the ranges above -- `127.0.0.1`, `[::1]`, this
   PC's LAN address; rebinding cannot produce an IP literal), `server.host` when it names one
   address or name, the host of `server.public_url` when set, or an entry of
   `server.allowed_hosts`. A missing or other `Host` gets 421 (HTTP) or a 1008 close before
   `accept()` (WebSocket), and never reaches a route or the bearer check. A capture client that
   reaches the PC by a name (`mypc.local`) needs that name in `allowed_hosts` or `public_url`.
3. **Bearer check** (`server/auth.py`, `BearerAuthMiddleware`): every HTTP route except
   `EXEMPT_ROUTES` -- `GET /api/health`, `POST /api/pair`, `POST /api/pair/codes` -- needs
   `Authorization: Bearer <token>` of a paired device, else 401 with `WWW-Authenticate: Bearer`.
   A loopback client passes without a token while `server.trust_localhost` is true, so the web UI
   works on the PC itself. This includes the static web app: a browser on another machine gets
   401 for `/`. Whoever passed is in `request.state.principal` (`Principal(device_id, local, protocol_version)`: the version the device sent at pairing, this backend's own for the PC).

**WebSocket routes** are not covered by the bearer middleware. Each one calls
`await authenticate_websocket(websocket)` (`server/auth.py`) before `accept()`. It reads the token
from `Authorization: Bearer <token>` or from the `?token=` query parameter (browsers cannot set
WebSocket headers), applies the same loopback trust, and returns the `Principal`. If
authentication fails, it closes with 1008 and returns `None`, and the route must just return.

**Paired devices** (`server/devices.py`, `DeviceStore`): `issue_token(name, protocol_version)`,
`verify_token(token)`, `list_devices()` and `revoke(device_id)` over the JSON file at
`server.devices_path` (default `~/.local/share/studentassistant/devices.json`, never inside the
vault). The file is written atomically with mode 600. It keeps, per device, an id, the name, the
creation time, the `protocol_version` of its pairing request (read as `1.0` when a record
predates it) and a salted SHA-256 hash of the token (compared with `hmac.compare_digest`), never
the token itself. A token is `sa_` + `secrets.token_urlsafe(32)`. The file is re-read on every
check, so a revocation from the CLI takes effect in the running server immediately.

**Log hygiene** (`server/redaction.py`): `create_app` calls `install_log_redaction()`, which wraps
the process's log record factory. Every record, uvicorn's access and error logs included, then has
its message, string arguments and traceback redacted: `Bearer ...`, `token=...`, `sa_...`
tokens, `XXXX-XXXX` codes, and the JSON fields `token` / `pairing_code`. A 422 validation error
never echoes the request's `input` back.

### Session lifecycle -- `server/sessions.py`

`SessionService(bus, *, vault=None, sync=None, vault_settings=None, host=None, sync_interval=1.0,
end_hook_timeout=10.0, index_interval=5.0)` (on
`app.state.sessions`) owns subjects/topics listing and creation and the session state machine
`active` -> `ended`, over the vault's public functions (every call in a worker thread; lifecycle
changes serialised by one lock). On first use it opens the vault (lazily, when built without one),
pulls it (`GitSync.sync()`, in a worker thread) and then scans it for unended sessions, so a session
another PC left open is seen.

- A session belongs to exactly one topic of one subject, fixed at start (ADR-0003). There is no
  topic switch: switching topic is ending the session and starting another.
- At most one active session per backend. `start` is refused (`ActiveSessionExistsError`, which
  names the session in `.session_id`) while any session is unended -- the active one, or one an
  earlier run left unended, which must be resumed or ended first; `resume` is refused while a
  different session is active. Resuming the active session again (a client reconnect) is allowed.
- `start` pulls the vault first (`GitSync.sync()`, in a worker thread, under the lifecycle lock).
  A `conflict` outcome refuses the start with `VaultSyncConflictError` (a `SessionConflictError`;
  `.conflicts` lists the paths, which the message names too) and starts nothing; nothing is
  auto-resolved (ADR-0002). `offline`, `auth` and `error` are logged and the start proceeds
  (offline-first). A conflict at vault open is only logged; the next start refuses on it.
- `startup()` / `shutdown()` (the app's lifespan): between them an open vault has the background
  `GitSync.run(sync_interval)` task (`sync_running` says whether it runs); `shutdown()` cancels
  it and flushes (`GitSync.flush()` in a worker thread). No git call runs on the event loop.
- The derived search index (ADR-0002, `vault.index.VaultIndex`): right after the vault's first
  pull and scan, `VaultIndex.open(vault, vault_settings.index_path)` runs in a worker thread
  (rebuilding the index when it was built for another HEAD, e.g. after that pull); `index` is it,
  or `None` before the vault opens or when it cannot be opened (logged; the vault still works and
  search answers 503). While serving, `VaultIndex.run(index_interval)` runs next to the sync loop
  (`index_running`), updating the index in a worker thread. After the pull of every session start
  (unless it conflicted) a `refresh()` is scheduled in a worker thread, without delaying the start
  (`await wait_index_refreshed()` waits for it). `shutdown()` cancels the loop, waits for a
  running refresh and closes the index (in a worker thread) after the flush.
- `resume` continues the session's logs: the next event's `seq` is one past the last in its
  `events.jsonl` (the vault's `resume_session`).
- Lifecycle events are published on the bus as persisted events with origin `user`:
  `session.started` (`subject_id`, `topic_id`, `client_time_ms`, `device_id`),
  `session.resumed` (`device_id`) and `session.ended` (`client_time_ms`, `reason`, `device_id`);
  `device_id` is `null` for a loopback client without a token. `LIFECYCLE_KINDS` lists them.
- `end` publishes `session.ended`, records `ended_at` (`end_session`), detaches the session from
  the bus, then forces a vault checkpoint (`GitSync.checkpoint("sesión <id> terminada")`) and a
  push (`push_now`). A failed commit or push is left in `GitSync.status()`, never raised.
- End hooks (`EndHook`: an async callable of the session id), run by `end` in the order added,
  while the session is still attached to the bus: `add_before_ended(hook)` before `session.ended`
  is published (for a tail that must still be logged as events, e.g. flushing the server-side STT
  provider, #136), and `add_before_close(hook)` after it is published and before `end_session`.
  Each is bounded by `end_hook_timeout`; a hook that fails or times out is logged and the session
  ends anyway. A hook must not call back into the `SessionService` lifecycle (its lock is held).
  A hook cut off by the timeout keeps nothing it could still publish: the session is ended and
  detached, so its later events are refused. The observer's flush does not depend on the timeout
  for correctness: a batch it could not land is caught up in the next session of the topic (or
  the resume), see `docs/modules/observer.md` (catch-up, #176).
  The app adds `TranscriptPipeline.drain()` as a before-close hook, so every `transcript.final`
  published before `session.ended` is in `transcript.jsonl` before the session is marked ended,
  then the observer's `DigestOnEnd` (#56), which regenerates the topic's `state/digest.md` from
  the log (`session.ended` included) before the end's checkpoint, with or without Claude. The
  digest reader `observer.topic_digest` is what the app gives the live observer (`digest=`) and
  the notes route gives `generate_notes`.
- Every vault write it makes, and every persisted bus event, calls `GitSync.note_change()`.
- `await open_vault()` -> the `Vault`, opened (pulled and scanned) on first use like every other
  call, or `VaultUnavailableError`: what the read routes read through.
- `add_on_open(hook)`: `hook(vault)` is called (synchronously, on the event loop; it must only
  schedule work) once the vault is first opened, pulled and scanned. The app registers the page
  transcriber's `catch_up_vault` there (server-start catch-up, #181, `docs/modules/sources.md`).
- For other server code (the WebSocket gateway): `active` -> `OpenSession | None`
  (`session_id`, `subject_id`, `topic_id`, `started_at`, `started_at_ms`) and
  `get_active(session_id)`, the active session only when it is that one.
- For the routes that write into the active session (captures): `await
  require_active(session_id)` -> the vault `Session` handle, raising `UnknownSessionError`,
  `SessionAlreadyEndedError`, `SessionConflictError` (unended but not resumed) or
  `VaultUnavailableError`; `note_change()` tells `GitSync` about a vault write. The module-level
  `stored_captures(session)` -> `{capture_id: payload}` of the session's `capture.stored` events
  (blocking: call it in a worker thread).
- Refusals are `LifecycleError`s: `UnknownSessionError`, `SessionConflictError` (with
  `ActiveSessionExistsError`, `SessionAlreadyEndedError` and `VaultSyncConflictError` under it) and
  `VaultUnavailableError`; an unknown subject or topic is the vault's `SubjectNotFoundError` /
  `TopicNotFoundError`.

### Session WebSocket -- `server/ws.py`

`WS /ws/sessions/{session_id}`, protocol v1 (`protocol/README.md`), served by the
`SessionGateway(bus, sessions, stt, *, sink_factory=..., provider_factory=..., clock=...)` on
`app.state.gateway` (`ws_router()` mounts it). `sink_factory(stt, clock_offset_s)` builds each
connection's `TranscriptSink` (default `InMemoryTranscriptSink`, whose `clock_offset` is the
client-clock reading at session start in seconds); `provider_factory(stt)` builds a session's
server-side provider (default `provider_from_settings`); `clock()` is backend epoch ms. Tests
replace all three.

- **Before `accept()`**: the LAN guard and the Host allowlist (1008), then
  `authenticate_websocket` (1008 without a valid token or loopback trust).
- **Session check** (after `accept()`, so the client can read the reason): a `session_id` that is
  not the active, attached session (unknown, ended, or unended but not resumed) is closed with
  `CLOSE_UNKNOWN_SESSION` (4404) and a reason; nothing is published. A session that ends while a
  socket is open is closed with 4404 on the socket's next publish.
- **Handshake**: the first message must be `hello` (anything else, a binary frame included, is
  refused). An incompatible MAJOR closes with 1008 and the `check_compatible` message as reason,
  and no `hello.ack`. Otherwise `hello.ack` carries `negotiate(hello.protocol_version)`,
  `stt_mode` = `stt.mode` of the config (never the client's `capabilities.stt`), `audio_format`
  (`pcm16`, 16000 Hz, mono) exactly in `server` mode, `clock_offset_ms` = backend clock minus
  `hello.client_time_ms`, and `server_time_ms`. Client times map to backend time by adding the
  offset and to session time by subtracting the session's `started_at_ms` (clamped at 0). Each
  (re)connection takes a new offset from its own `hello`. In `server` mode, a session that
  already received audio also gets an `ack` with its highest contiguous `audio_seq` right after
  `hello.ack`, so a reconnecting client knows where to resume.
- **Validation**: every text message is parsed with `parse_client_event`. Non-JSON, an unknown or
  missing `type`, an invalid message, a second `hello`, `transcript.client.*` in server mode or a
  binary frame in client mode closes the socket with `CLOSE_PROTOCOL_VIOLATION` (1008) and a
  reason (at most 123 bytes); none is ignored and nothing of it is published.
- **Client mode**: each `transcript.client.partial` / `.final` becomes a `ClientSegment`
  (client-clock seconds) ingested by the connection's sink. Segments are de-duplicated by
  `segment_id` per session: once a final has been handled, a repeated final or a later partial of
  that id is dropped, so resending after a reconnect is idempotent.
- **Server mode**: binary frames go through `decode_frame`; a wrong magic, another MAJOR or a
  malformed frame is refused (1008). Frames are de-duplicated by `seq` and fed in `seq` order,
  starting at 0, as `AudioChunk`s in session time to the session's provider; frames ahead of the
  next expected `seq` wait in a buffer of at most `MAX_PENDING_FRAMES` (512), past which the
  socket is closed so the client resends from its last ack. After each frame the backend sends an
  `ack` with `audio_seq` = the highest contiguous `seq` received (none until frame 0 arrived).
  Provider segments get backend ids `server-<n>` (a partial and the final after it share one).
  The provider is built at the session's first server-mode `hello`; a failure closes with 1011.
- **Resume**: the per-session receive state (finals seen, next audio `seq`, the out-of-order
  buffer, the provider) outlives the socket and is kept until another session connects, so a
  client that reconnects and resends from the acknowledged `seq` makes no gap and no duplicate.
  Several sockets of one session are serialised on that state.
- **Published on the bus** (`persist=True` unless noted):
  - `transcript.final` (origin `stt`, `t` = `session_start_ms`) and `transcript.partial`
    (origin `stt`, a notice: `persist=False`), payload `segment_id`, `session_start_ms`,
    `session_end_ms`, `text`, `language` (the client's in client mode, `stt.language` in server
    mode), `provider` (`stt.provider`) and `confidence` when known. `transcript.final` with
    `payload.segment_id` is what the observer fold registers. A final the vault refuses as
    secret-looking is logged, not stored, and counts as handled.
  - `button` (`button`, `source?`), `marker` (`label?`) and `command.ack` (`command_id`) for the
    client `ack`, origin `phone`, each with `client_time_ms`, `backend_time_ms` (client time +
    offset) and `t` = its session time.
- **Forwarded to the client**: each connection subscribes to its session's `FORWARDED_KINDS`
  (`transcript.partial`, `transcript.final`, `command`, `notice`, `capture.stored`) before
  `hello.ack` and sends each as the matching server message (`transcript.*` from the payload
  fields above; `command` from `command_id`, `command`; `notice` from `pending_count`;
  `server_time_ms` from the payload or the clock). A persisted `capture.stored` (the capture
  upload stored a burst) becomes the capture `ack`: `ServerAck` with `capture_ids:
  [payload.capture_id]` and `server_time_ms` from the clock, never with `audio_seq`; a
  `capture.stored` notice is not acknowledged. A client connected when a capture is stored gets
  exactly one ack for it; a duplicate upload publishes nothing, so it gets none. Past acks are
  not replayed on (re)connect: a reconnecting client learns the stored captures from
  `received_capture_ids` in the resume response. A payload that makes no valid message is logged
  and skipped. The subscription is closed when the socket ends, however it ends.
- **Backpressure**: vault appends run in worker threads through the bus, so nothing blocks the
  event loop; inbound messages are handled one at a time in order. Outbound traffic goes through
  the connection's bounded bus subscription, which drops the oldest notices (partials) when the
  client reads slowly and never a persisted event.
- **Ending**: the gateway registers `end_session(session_id)` as a `SessionService`
  `add_before_ended` hook, so it runs before `session.ended` is published (and so before the
  pipeline drain and the vault end). Under the session's receive-state lock it flushes the
  server-side provider (`finish()`), publishes the returned segments as `transcript.final` /
  `transcript.partial` exactly like the ones audio produced (same `server-<n>` ids), and drops the
  session's receive state, even when the flush fails or times out (the hook runner logs it). It
  only publishes on the bus, never calls back into `SessionService` (the session lock is held).
  A socket of the session still open then handles no further message (closed with 4404), and a
  new socket of it is refused, although `end` still has it attached.

### Recording and replay -- `server/recording.py`, `server/recorder.py`, `server/replay.py`

A **recording** is a directory holding what one capture client sent during one session, and
nothing the backend derived from it (VISION §5 item 11, the session simulator). It is never vault
content: `serve --record` writes it under `[server].recordings_dir`, and `replay` reads it from
any path. It is read and written only through `server/recording.py`, whose lines and manifest are
the protocol's own Pydantic models, so a recording holds exactly what the wire carries, with the
client times the client gave it.

| file | holds |
|---|---|
| `manifest.yaml` | `RecordingManifest`: `format_version` (`1`), `subject` and `topic` (REST ids), `language`, `stt_mode` (`client` or `server`), `stt_provider` (the client recognizer `hello` announces, default `replay`; client mode only), `started_client_time_ms` (client-clock epoch ms of the session start; every other client time is relative to it) |
| `transcript.jsonl` | client mode: one `transcript.client.partial` / `transcript.client.final` message per line, with its client times |
| `audio.wav` | server mode: PCM16, 16000 Hz, mono, sample 0 at the session start |
| `events.jsonl` | one `button` or `marker` client message per line, with its `client_time_ms` |
| `captures.jsonl` | one `rest.sessions.captures.request` metadata object per line (`capture_id`, `trigger`, `client_time_ms`, `images`, ...), in upload order |
| `captures/` | the images of those bursts, `<capture_id>.<part>.<ext>` (`.jpg`, `.png` or `.webp` from the image's `content_type`) |

Every file but the manifest is optional (absent = empty). A client-mode recording must not hold
`audio.wav` and a server-mode one must not hold transcript lines.

- `read_recording(directory) -> Recording` validates the whole directory up front (manifest,
  every line, every capture image present, the WAV format) and raises `RecordingError` (a
  `ValueError`) on anything it cannot read, so a replay never starts on a half-valid recording.
  `read_manifest(directory)` reads the manifest alone. `Recording` holds `directory`,
  `manifest`, `transcript`, `events`, `captures` (`RecordedCapture`: `metadata`, `image_paths`),
  `audio_path` (`None` without audio) and `read_audio()`.
- `RecordingWriter(directory, manifest)` writes the manifest on creation, then
  `append_transcript`, `append_event`, `add_capture(metadata, images)` (images by `part`; the
  parts must be exactly the ones the metadata names, else `RecordingError`), `append_audio(pcm)`
  and `close()` (finishes the WAV header); also a context manager. Nothing is fsynced: a
  recording is a development aid. `write_wav` / `read_wav` / `WavWriter` handle PCM16 16 kHz
  mono WAV files, refusing any other format.
- The test fixture `backend/tests/fixtures/sessions/sample/` is a tiny synthetic client-mode
  recording (Spanish finals and partials, one `switch_source` button, one generated page image),
  regenerated by `backend/tests/fixtures/sessions/make_sample.py`.

**Recorder** (`SessionRecorder(root)`, on `app.state.recorder`): the WebSocket gateway and the
capture endpoint call it with exactly what the client sent and the backend accepted -- the client
transcript messages (a resent final is not recorded twice), the audio frames in `seq` order as
they are fed to the provider (reassembled into `audio.wav`, preceded by silence back to the
session start so a sample's position stays its session time), the `button`/`marker` messages, and
each newly stored capture burst with its images (a duplicate upload records nothing). Each session
gets `<recordings_dir>/<session_id>/`; the manifest is written at the session's first completed
`hello`, when the client clock offset is known (a capture uploaded before any `hello` assumes the
client clock is the backend's). A session resumed after a backend restart records into
`<session_id>-2/` (and so on), so no recording is overwritten. The recording is closed when its
session ends, and every open one when the app shuts down. All recorder I/O runs in a worker
thread, and a recording that cannot be written is logged and never fails the socket or the
upload.

**Replay** (`await replay(recording, transport, *, speed=1.0, subject=None, topic=None,
sleep=asyncio.sleep, clock=time.monotonic, confirm_timeout_s=5.0, ack_timeout_s=10.0) ->
ReplayResult`) acts as a capture client over exactly the live path:

1. ensures the subject and topic exist (`GET`/`POST /api/subjects...`, the id as the name when it
   creates one), then `POST /api/sessions` with `client_time_ms = started_client_time_ms`;
   `subject`/`topic` override the manifest's;
2. connects `WS /ws/sessions/{id}`, sends `hello` and waits for `hello.ack`, refusing a backend
   whose STT mode is not the recording's;
3. sends, in client-time order and each at its offset from the session start divided by `speed`:
   every transcript message (due at its `client_end_ms`), every `button`/`marker` (at its
   `client_time_ms`) and every capture burst as a multipart `POST /api/sessions/{id}/captures`
   (at its `client_time_ms`). A server-mode recording sends `audio.wav` instead, sliced into
   `AUDIO_FRAME_MS` (100 ms) protocol binary frames (`encode_frame`, `seq` from 0, each due when
   its last sample was captured);
4. `POST /api/sessions/{id}/end` at the last recorded time, then closes the socket.

Before each upload and before the end it settles: it waits until the backend has processed every
WebSocket message sent so far (so a capture is stored under the source context of the
`switch_source` buttons before it) and has echoed every final (up to `confirm_timeout_s`, since a
final refused as a secret is never echoed); in server mode also for the `ack` of the last frame
sent (up to `ack_timeout_s`). When a server-mode socket drops, it reconnects (up to
`MAX_RECONNECTS`, 3), resends `hello` keeping the first connection's clock offset, and resends
every frame after the highest acknowledged `audio_seq`. `speed <= 0` is a `ValueError`; a refused
request, a closed socket, a mismatched STT mode or a socket that keeps dropping raise
`ReplayError`. `ReplayResult` counts what was sent (`partials_sent`, `finals_sent`,
`events_sent`, `captures_stored`, `captures_duplicate`, `audio_frames_sent`,
`audio_frames_resent`, `reconnects`) plus `session_id`, `subject_id`, `topic_id`, `ended_at_ms`.

Pacing reads the injectable `clock` and waits with the injectable `sleep`, so tests replay without
real waiting. The backend is reached through a `ReplayTransport`: `AsgiTransport(app,
lifespan=True)` drives an in-process `create_app` app through ASGI as a trusted loopback client
(running its lifespan like `serve`), `HttpTransport(base_url, token=None)` talks to a running
server over HTTP and `websockets` (a LAN address needs a device bearer `token`; a loopback URL is
trusted without one).

### Session event bus -- `server/bus.py`

`SessionBus(on_append=None, default_queue_size=256)` (on `app.state.bus`) is the in-process
publish/subscribe every session event goes through; stt, sources, observer, editor and the
WebSocket gateway publish and subscribe here.

- `attach(session)` / `detach(session_id)` / `is_attached(session_id)`: the lifecycle service
  attaches the vault `Session` handle of the active session; publishing for any other session is
  refused (`SessionNotAttachedError`, a `BusError`). `attached(session_id)` returns that handle
  (None when not attached): the app gives it to the stt `TranscriptPipeline` as its lookup.
- `await publish(session_id, kind, origin, payload=None, *, persist=True, t=None) -> BusEvent`.
  A persisted event is appended to the session's `events.jsonl` through the vault handle (never
  written by the bus, ADR-0002; the write runs in a worker thread), gets the log's next `seq`, and
  only then is delivered; a refused append (`SecretRefused`, `SessionEndedError`) delivers
  nothing and leaves `seq` as it was. `persist=False` publishes a **notice** (a transcript
  partial, a pending count): delivered, never written, `seq` `None`. `t` defaults to the session
  time now (ms since `started_at`). Publishes are serialised, so every subscriber sees events in
  `seq` order and notices in publish order among them.
- `subscribe(*, name="", session_id=None, kinds=None, maxsize=None) -> Subscription`: every event
  published from then on that matches the filter (one session, a set of kinds; `None` = all).
  Read with `await sub.get()`, `sub.get_nowait()` or `async for event in sub`; `sub.close()` (or
  leaving `with` / `async with`) releases it, and iteration ends once it is closed and drained.
  `bus.close()` closes every subscription.
- Slow subscribers never block publishers: delivery only appends to the subscription's bounded
  queue. When it is full, the oldest queued notice is dropped (counted in `sub.dropped`; a notice
  arriving at a queue that holds no notice is itself dropped). A persisted event is never dropped:
  a queue holding only persisted events grows past its bound (`sub.overflowed`, one warning per
  episode).
- `BusEvent` (frozen): `session_id`, `subject_id`, `topic_id`, `kind`, `origin` (`phone`, `stt`,
  `observer`, `editor`, `user`), `t`, `payload` (shared by every subscriber: never mutate it),
  `seq` (`None` for a notice), `schema_version`, and `persisted`.

### CLI

- `studentassistant serve [--record]`: runs the app on `server.host`:`server.port` (uvicorn with
  `proxy_headers=False`). `--record` gives the app a `SessionRecorder` over
  `server.recordings_dir` and records every session there (see "Recording and replay"); it exits
  with code 1 when that directory is inside the vault. Without `--record` nothing is recorded.
- `studentassistant replay <dir> [--speed 1.0] [--topic <subject>/<topic>] [--url <base url>]`:
  reads the recording in `<dir>` (exit 1 with the `RecordingError` when it cannot) and replays it
  as a capture client. `--speed` divides every recorded offset (`4` replays twenty minutes in
  five; must be > 0), `--topic` overrides the manifest's subject and topic. With `--url` it talks
  to that running backend (no token option: use a loopback URL); without it, it builds the app
  in-process from the configuration (the configured vault and `[stt]`, whose mode must match the
  recording's) and runs its lifespan. Prints (in Spanish) the session id and what was sent; a
  `ReplayError` exits 1, a malformed `--topic` or `--speed` exits 2.
- `studentassistant pair`: asks the running backend for a code (`POST /api/pair/codes` on
  `127.0.0.1:<server.port>`, or on `server.host` when that names one address) and prints a QR of
  the JSON `{"url", "code"}` in the terminal (segno, compact), plus the URL, the code and its expiry.
  Since minting is loopback-only, `pair` needs the default wildcard bind (or `127.0.0.1`).
- `studentassistant devices` / `devices list`: the paired devices (id, name, paired-at; never a
  token). `studentassistant devices revoke <id>` removes one, and its token stops being accepted.

### Config keys (`[server]`, or `SA_SERVER__*`)

| key | default | meaning |
|---|---|---|
| `host` | `0.0.0.0` | bind address of `serve` |
| `port` | `8765` | bind port of `serve` |
| `trust_localhost` | `true` | loopback clients need no bearer token |
| `devices_path` | `~/.local/share/studentassistant/devices.json` | the paired devices file |
| `public_url` | unset | the base URL put in the pairing QR (default: LAN address + port); its host is also an allowed `Host` |
| `max_capture_image_bytes` | `15728640` (15 MiB) | largest image part a capture burst may carry (413 beyond) |
| `max_capture_images` | `5` | most images one capture burst may hold (413 beyond) |
| `allowed_hosts` | `[]` | extra names a request's `Host` may carry (the DNS-rebinding allowlist above); env as JSON, `SA_SERVER__ALLOWED_HOSTS='["mypc.local"]'` |
| `recordings_dir` | `~/.cache/studentassistant/recordings` | where `serve --record` writes one recording directory per session id (`~` expanded); must not be inside the vault |
