# Module: server

**Lives in:** `backend/src/studentassistant/server/` and the CLI entry points in
`studentassistant/cli.py`.

## Responsibility
- FastAPI app factory, LAN bind, pairing + bearer auth (ADR-0001), static web assets.
- The **active user** of every request (protocol 1.8, #549, epic #544): `server/user_scope.py`
  turns `X-SA-User` / `sa_user` into that user's own vault handles, and the users REST API
  (`server/user_routes.py`) lists, adds and edits the students this backend serves and their
  profile photo.
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
given), `observer` (the `ObserverLoop`, `None` without an `llm_transport`), `notes` (the
`NotesGenerator` of the notes generation route, `None` without an `llm_transport`), `workspace`
(the `WorkspaceHub` of the workspace stream, always), `assistant_requests` (the
`AssistantRequestConsumer`, `None` without an `llm_transport`) and `doubt_chat` (the `DoubtChat`
that asks the doubts in the workspace chat, always; below).
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
- **The users themselves** (`server/user_routes.py`, `users_router()`, protocol 1.8, #549): what a
  client's selection screen reads and what «Editar perfil» writes, registered right after pairing
  and, like it, **never user-scoped** -- these routes work on the vault's ROOT handle (the one
  `SessionService.open_vault()` gives), never on `vault.for_user(...)`, so an `X-SA-User` header or
  an `sa_user` cookie on one of them is read by nobody and a stale or bogus selection cannot stop a
  client from listing the users to choose from (see "Active user" below). Bodies are the five
  `rest.users.*` messages of `protocol/README.md` "Users (1.8)"; a `User` is `{id, name, email?,
  photo_url?}`, with the optional ones left out rather than `null` (`response_model_exclude_none`).
  Every write calls `SessionService.note_change()`, so the background `GitSync` commits and pushes
  it, and every vault call runs in a worker thread (one blocking step per request: the writers
  fsync and the photo's decode/resize/encode is CPU-bound). They sit behind the bearer check like
  every `/api` route but health and pairing, and a cookie-authenticated write passes
  `same_site_origin` exactly like every other cookie write. A vault that cannot be opened is 503
  (`"No se puede abrir la bóveda."`) on every one of them.
  - `GET /api/users` -> `rest.users.list.response`, every user in `vault.list_users`' order (by
    name, compared case-insensitively, and then id). A `profile.json` that cannot be read back, or
    one that parses but is no `User` the protocol can carry (a hand-edited name over
    `USER_NAME_MAX_CHARS`, 80), is 500 with a Spanish `detail` («No se puede leer el perfil de un
    usuario de la bóveda.»).
  - `POST /api/users` (`rest.users.create.request`) -> 201 the created `User`
    (`rest.users.create.response`): `vault.create_user` picks the id (`slugify(name)`, plus a
    numeric suffix when that folder is taken) and creates the folder and its `profile.json`. A name
    or an email the vault refuses is 422 with the vault's Spanish message; a folder already there
    (two creates of the same name at once) is 409.
  - `PATCH /api/users/{user_id}` (`rest.users.update.request`) -> the updated `User`
    (`rest.users.update.response`). A field left out keeps what the user has, an `email` of `""`
    clears it, and a body carrying neither is refused by the model itself: the three are
    `UserUpdateRequest`'s and `vault.update_user`'s own reading of these two values, so the route
    passes the body straight on. The id never changes when the name does. An unknown user is 404
    `user_not_found`; a name or an email the vault refuses is 422 with its Spanish message.
  - `PUT /api/users/{user_id}/photo` -> the updated `User`. The body is the raw image, not JSON,
    and its `Content-Type` must be one of `USER_PHOTO_CONTENT_TYPES` (`image/jpeg`, `image/png`,
    `image/webp`); another one is 415 with the Spanish sentence the vault writes for the same
    refusal, answered before a byte of the body is read -- as the user lookup is, so a stale id
    costs nobody an upload. The body is read as it streams in, never buffered whole, and refused
    with 413 past `[server] max_user_photo_bytes` (a `Content-Length` over the cap is refused
    before anything is read; the running total is what catches a body that declares no length or
    lies about it). What lands in the vault is `vault.set_user_photo`'s downscaled `photo.jpg`,
    never the bytes that arrived. An image that does not decode, an empty body, one over the
    vault's own `MAX_USER_PHOTO_BYTES` ceiling (5 MiB, which no configuration lifts, so a raised
    `[server]` cap lets the bytes by the 413 and into this Spanish 422) or one the secret guard
    refuses («La foto parece contener una clave o un token y no se ha guardado.») is 422.
  - `DELETE /api/users/{user_id}/photo` -> the updated `User`, now with no `photo_url`. Removing a
    photo that is not there is not an error: «Quitar foto» says the profile has none, which is
    already true, so a repeated request gets the same answer.
  - `GET /api/users/{user_id}/photo` -> `image/jpeg` (the one type stored, whatever came in), with
    `Cache-Control: no-cache` (a browser revalidates an avatar instead of trusting its copy) and
    `X-Content-Type-Options: nosniff`; 404 `user_not_found` for an unknown user and 404 with a
    Spanish `detail` («El usuario no tiene foto de perfil.») for one with no photo.
  - `photo_url` is `/api/users/<id>/photo?v=<first 12 hex of the stored photo's SHA-256>`
    (`PHOTO_VERSION_HEX_CHARS`), so the URL changes exactly when the image behind it does and a
    client that cached the previous avatar cannot keep showing it. A profile naming a photo its
    folder has lost gives no URL at all: pointing a client at a 404 is worse than an empty avatar.
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
    The end body's `prepare_notes` (protocol 1.6, #258) is still accepted, so an old client
    that sends it keeps working, but it is ignored since #440: ending a session never starts a
    notes generation and the response never has `notes_generation`. The notes are prepared from
    the workspace chat ("prepárame el tema", `assistant_requests.py`) or `POST .../notes/generate`.
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
  - Right after `capture.stored`, a persisted `capture.triaged` event (#324, origin `sources`,
    ADR-0003, #360: `captures.TRIAGE_ORIGIN`; `sources.triage.CAPTURE_TRIAGED_KIND`, which the
    observer's fold ignores) carries the capture's triage, stored in its sidecar by
    `store_capture`: `capture_id`, `source_path`, `source_id` (topic-relative), `status`
    (`kept` | `flagged` | `set_aside`), `reasons`, `duplicate_of`, `decided_by`,
    `capture_session_id`. When the new capture is a sharper repeat that displaced an older one,
    a second `capture.triaged` sets the older one aside. With `[sources] triage_llm_check` and an
    LLM transport (`app.state.triage_client_factory`, role `transcriber`), the burst is processed
    and triaged first and the checks near a threshold are asked to Claude, bound to the session's
    ledger, before it is stored (`sources.triage_llm`). A student's decision
    (`sources.set_capture_triage`, from the chat routing of #327) is published by its caller as
    `capture.triaged` with origin `user`.
  - Idempotent on `capture_id`: the stored ids of a session are its `capture.stored` events
    (`sessions.stored_captures(session)`, reading `events.jsonl`, so it survives a restart). A
    stored id is answered 200 `status: "duplicate"` with the stored `image_count`, storing and
    publishing nothing. The check and the store are serialised per session, so concurrent
    uploads of one new id store it once. A session that ends between the check and the event is
    409 (the source file may stay; the observer never sees it without its event).
- `GET /api/vault/status` (`server/vault_status.py`, web-only, not phone protocol; user-scoped like the
  session routes, #550, and `host_warning` also names the `user` capturing) -> the vault's
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
- `GET /api/sessions/{id}/health` (`server/session_routes.py` + `server/session_health.py`, #262,
  web-only, not phone protocol) -> `SessionHealthResponse` `{session_id, ok, observer,
  observer_paused, observer_paused_message, transcription, push}`, each of `observer`,
  `transcription`, `push` a `{count, message}` whose `message` is the latest failure in Spanish
  (`null` while `count` is 0). `observer` counts the observer's failed calls (its transient
  `observer.call_failed` notices, `kind` -> "Claude no responde (sin conexión o saturado)",
  "Claude ha rechazado la petición", ...); `transcription` the session's
  `page.transcription_failed` events (`reason` `cost_cap` / `refused` / `error`);
  `observer_paused` whether the latest `observer.status` is `paused` (a cost cap; the message
  names the `session` or `day` cap). `push` is the vault's current push streak, not per
  session: `GitSync.status().consecutive_push_failures` and `last_push_failure.kind` (`offline`
  -> "no hay conexión con GitHub", `auth`, `rejected`, `error`), cleared by a successful push.
  `ok` is true when every count is 0 and the observer is not paused. The counts live in memory
  (`app.state.health`, a `SessionHealth` holding one bus subscription made with the app, folded
  on each request; no task runs), start at 0 on a restarted backend and are forgotten on
  `session.ended`, so an ended session answers zeros. An id no topic lists is 404 (`no existe la
  sesión <id>`), a path id outside the protocol's id pattern 422, a vault that cannot be opened
  503. Bearer check like every non-exempt route.
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
- `GET /api/subjects/{s}/topics/{t}/cost` (`server/cost.py`, `topic_cost(vault, s, t)`, #260) ->
  `TopicCost` `{subject_id, topic_id, total, sessions, no_session}`: the topic's ledger
  (`studentassistant.vault.read_ledger`) summed. Every totals object is `{usd, tokens,
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, calls, unpriced_calls}`
  (`tokens` the four kinds added; `usd` adds each entry's `estimated_usd` and leaves out the
  `unpriced_calls`, whose model had no price). `sessions` has one row per session the topic lists
  (spent or not) and per session id only the ledger names, in id (start) order, each with
  `session_id` and `started_at_ms` (from `session.yaml`, else the session's first ledger entry);
  `no_session` sums the calls bound to no session (editor, generators). No price is computed in
  the server. Path ids follow the protocol's id pattern (422 otherwise); an unknown subject/topic
  404 (`"No existe ese tema en la bóveda."`), a vault that cannot be opened 503. The vault comes
  from `studentassistant.config`, read on every request; bearer check like every route.
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
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/sources` -> `TopicSources` (#323):
    `subject_id`, `topic_id` and `sources`, one `{vault_id, kind, title}` per stored source in
    `vault.list_sources` order (by kind, then number): `vault_id` is the vault-relative path the
    source routes below take, `kind` one of `notes`, `book`, `pdf`, `web`, `images`, and `title`
    comes from the sidecar (`title`, as a web snapshot has, else `original_name`, as a PDF has;
    `null` without either). An empty topic lists `[]`; reads only.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/sources/status` -> `TopicSourcesStatus`
    (#326): `subject_id`, `topic_id` and `sources`, one `SourceStatus` per stored source
    (`editor.incorporate.source_status`, `docs/modules/editor.md`): `source_id` (topic-relative,
    `sources/notes/page-003.jpg`), `kind`, `number`, `label` («la página 3»), `state`
    (`pendiente` | `incorporada` -- the current notes cite it -- | `apartada` -- set aside by
    capture triage) and `reason` (Spanish, only `apartada`: «borrosa», «repetida de la página
    1»), in catalogue order (notes pages, book pages, PDFs, web pages, pasted images). Unknown
    topic 404; reads only. It is what the chat router (#327) resolves "la página 3" against.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/notes` -> `TopicNotes` (`subject_id`,
    `topic_id`, `text` of `notes/apuntes.md`, `version` as above, `revision`: the content
    revision token, `editor.notes_revision(text)`, SHA-256 hex, that a student save names as its
    `base_revision`); notes not written yet are 404 (`"Todavía no hay apuntes de este tema."`).
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
    so no source is ever rendered as a page of this origin. The one exception (#511): an SVG that
    is exactly `vault.sanitize_svg`'s output (`is_safe_svg`, re-checked on every read: a diagram
    the editor drew) is served as `image/svg+xml` under `default-src 'none'; style-src
    'unsafe-inline'; sandbox`, so the web's `<img>` can draw it; any other SVG stays octet-stream.
  - `GET /api/sources/{vault_id:path}/meta` -> `SourceMeta`: `vault_id`, `kind`, `media_type` (the
    one the content route serves), `size`, `meta` (the parsed sidecar, `null` without one) and
    `transcription` (the sidecar's `transcription` when it is text, else `null`) and `removed`
    (`true` once the student removed it, below).
  - `DELETE /api/sources/{vault_id:path}` (`server/source_routes.py`, `source_router()`, #451):
    the Recursos trash button. A **soft delete** ("retirar"): `vault.remove_source` marks the
    source `removed` in its sidecar and nothing is deleted. It leaves every listing built from
    `list_sources` (the topic's `sources`, `sources/status` and `summary` counts, the Recursos
    selection `check_selection` accepts, the editor's catalogue and context, the observer's
    sources list, triage, the transcription catch-up, the search index), while both GET routes
    above still serve it, so a citation of it in the notes keeps working. Cited sources may be
    removed (no 409): the footnote stays and resolves to the removed source. The change is
    committed at once (`Fuente <topic-relative id> de <s>/<t> retirada`); when the topic's session
    is the active one, a persisted `source.removed` event (origin `user`, payload `source_id`
    topic-relative and `source_path` vault-relative) goes on it (the observer's fold ignores it);
    with no live session the sidecar and the commit are the record. 204 with no body; a path that
    is not a topic's `sources/<kind>/<file>`, names a derived file or a sidecar, names nothing, or
    a source removed already is 404 (`"No existe esa fuente en la bóveda."`); an unreadable
    sidecar 409; an unopenable vault 503. Same LAN guard, Host allowlist and bearer check (loopback
    trust) as every web route.
  - `PUT /api/sources/{vault_id:path}/transcription` (`server/source_routes.py`, #473): the
    student's hand correction of a photographed page's transcription (the web's resource detail,
    «Editar la transcripción»). Body `{"text": "..."}` (at most 200 000 characters). What is
    pending in the vault is committed first (`Cambios pendientes antes de corregir una
    transcripción`), so the machine's text stays in git history; `vault.edit_page_transcription`
    then writes the text as `page-NNN.md` and records `transcription_edited: {at, by: student,
    previous_sha256, original_sha256}` in the sidecar, and the correction is committed at once
    (`Transcripción de <topic-relative id> de <s>/<t> corregida`). When the topic's session is the
    active one, a persisted `page.transcription_edited` event (`TRANSCRIPTION_EDITED_KIND`, origin
    `user`, payload `source_id`, `source_path`, `transcription_path`) goes on it; without a live
    session the sidecar and the commit are the record. 200 with `{source_path,
    transcription_path, text}` (the text as stored). 404 (`"No existe esa fuente en la bóveda."`)
    for anything that is not a listed `notes`/`book` page; 409 «Esta página todavía no está
    transcrita; no hay nada que corregir.» or «No se puede leer la ficha de esta página; no se ha
    guardado.»; 422 «La transcripción no puede quedar vacía.» or «El texto parece contener una
    clave o un secreto; no se ha guardado.»; 503 without a vault.
  - Both GET source routes go only through the vault's `read_source`: a path that is not a topic's
    `sources/<kind>/<file>` (absolute, `..` or `%2e%2e`, backslash, NUL, a symlink out of its
    directory) or names no file is 404 (`"No existe esa fuente en la bóveda."`); nothing outside
    the vault's sources is ever served.
- `GET`/`PUT /api/subjects/{subject_id}/topics/{topic_id}/book` (`server/book_routes.py`,
  `book_router()`, #58): the topic's textbook title (`vault.get_book`/`set_book`). `GET` answers
  `BookResponse` (`subject_id`, `topic_id`, `title`, `null` when none was set); `PUT
  {"title": "..."}` (at most 200 characters) records it and calls `SessionService.note_change()`.
  Unknown subject or topic 404 (`"No existe ese tema en la bóveda."`), empty title or one that
  looks like a key 422, an unopenable vault 503. No session needed.
- `GET /api/live` (`server/live_routes.py`, `live_router()`, #57): the web's live session view,
  streaming only the **requesting user's** active session (another user's is the empty snapshot
  and end of stream, #550),
  a read-only Server-Sent Events stream (`text/event-stream`, `Cache-Control: no-cache`) of the
  **active** session; web-only, not phone protocol. It opens with `retry: 3000`, then a
  `snapshot` (`LiveSnapshot`: `session` {`session_id`, `subject_id`, `topic_id`,
  `started_at_ms`} or `null`, `segments` -- the final transcript segments so far, `{segment_id,
  t_start, t_end, text}` in session ms --, `captures`, `outline`, `open_pending`). Without an
  active session the stream ends after that snapshot, so the browser's `EventSource` asks again
  three seconds later. Otherwise it subscribes to the session on the bus (before reading the log,
  skipping by `seq` what the log already held) and sends `segment` (a `transcript.final`),
  `partial` (a `transcript.partial` of a segment not final yet), `capture` (`LiveCapture`:
  `capture_id`, `t`, `page_path`, `source_path`, `source_context`, `status`
  `pending`/`transcribed`/`failed`, `text`, `page_number`, `message`; sent on `capture.stored`,
  `page.transcribed` and `page.transcription_failed`), `outline` (after each
  `observer.state_op`: the whole outline -- `{section_id, title, parent_id, segment_count}` in
  reading order, each section followed by its subsections -- and `open_pending`, from the
  observer's fold `load_observer_snapshot(write_back=False)` of the topic) and finally `ended`
  (`{session_id}`) on `session.ended`, where the stream ends. A `: keep-alive` comment goes out
  after 15 s of silence; a closed bus (shutdown) ends the stream. Writes nothing. Needs the bearer
  check like every non-exempt route.
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
- Web search (`server/web_search_routes.py`, `web_search_router()`, #59), through
  `app.state.web_searcher` (`sources.web_searcher.WebSearcher`, built with an `llm_transport` and
  `[sources] web_search_enabled`, started and stopped by the lifespan):
  `GET /api/subjects/{s}/topics/{t}/web-searches` -> `{"searches": [WebSearchRecord...]}` newest
  first (works without a transport); `POST` the same path with `{"query"}` (at most 500
  characters) -> 202 `{"search_id", "status": "queued"}`, the search running in the background
  (bound to the active session when it is of that topic); `POST
  .../web-searches/{search_id}/results/{index}/keep` -> 201 `KeptResponse` (`source_id`,
  `vault_id`, `title`, `url`) once the page is stored as `sources/web/NNN-<slug>.md`. Refusals in
  Spanish: unknown topic, search or result 404; no results yet or a reached cost cap 409; empty
  query, or a page that cannot be kept (a PDF, an error, a key) 422; Claude failure or refusal
  502; no transport, web search disabled, or no vault 503. See `docs/modules/sources.md`.
  The same router has `POST /api/subjects/{s}/topics/{t}/web-pages` (#62, a URL pasted in the web
  UI or shared to the phone): protocol `rest.topics.web_pages.create.request` (`url`, `via`?
  `url` | `share`) -> 201 `rest.topics.web_pages.create.response` (`source_id`, `vault_id`,
  `title`, `url`, `already_kept: false`), or 200 with `already_kept: true` when the topic already
  has that address (nothing fetched); `WebSearcher.keep_url`, bound to the topic's active session
  like a search. Refusals as for keeping a result (the cost cap with `code:
  cost_cap_reached`); a body that is not an http(s) `url` 422.
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
  active one (else it is only in `conversations/editor.jsonl`); a generation that wrote the
  notes (not a draft) is also a `notes.changed` (origin `generation`) on the workspace stream,
  however it was started. One generation per topic at a
  time. Errors, Spanish `detail`: no `llm_transport` 503 (`"La generación de apuntes no está
  disponible: ..."`), an unknown topic 404, a generation of the topic already running 409, a
  reached cost cap 409 `cost_cap_reached` (`"Se ha alcanzado el límite de gasto ... Confirma
  ..."`) until the body
  says `confirm_over_cap`, a Claude refusal or failure 502, a vault that cannot be opened 503.
  Needs the bearer check like every non-exempt route.
  **Batched mode** (#326, `[editor] prepare_mode = "batched"`, the default; `single` keeps the
  one-call `generate_notes` above): the generation is `editor.incorporate.incorporate_pending` --
  the topic's `pendiente` sources (notes pages first, then book, PDF, web) incorporated in
  sequential small batches of `[editor] incorporate_batch_size` (default 2, never more than
  `incorporate_max_sources`), each one editor call on the current notes plus only those sources,
  its own commit and chat entry. On the workspace stream each batch is a turn of kind
  `incorporate` (`turn.started`, `reply.delta`, `turn.result` with the `IncorporationResult`,
  `notes.changed` origin `editor`, or `turn.error`), followed by `incorporation.progress`
  `{done, total, source_ids}`; when something changed, the next notes version is tagged, the
  contradictions searched, and `notes.changed` origin `generation` closes the run. Its answer is
  still a `GenerationResult` (never a draft; `version`/`tag` only when the notes changed; the
  `warning` says what stopped it). A cost cap, a refusal or a failed batch stops the run with the
  done batches kept (calling again continues with what is pending); on the very first batch it is
  the error above (a cap 409 `cost_cap_reached`, a refusal or failure 502). Nothing pending: no
  call, `version` `null` and a `warning`. The topic digest is not part of an incorporation.
- **`NotesGenerator.incorporate(sessions, subject, topic, source_ids, *, on_reply=None,
  request=None, turn_id=None, confirm_over_cap=False) -> IncorporationResult`** (#326): one
  incorporation of a few sources ("incorpora la página 3") for the chat router (#327), which
  claims the topic's notes lock first (`TURN_HOLDER`) and publishes the turn itself (a
  `TurnBroadcast` of kind `incorporate`). It passes `[editor] incorporate_max_sources`, the bus
  publisher (`notes.incorporated`, origin `editor`, when the topic's session is active) and the
  live-session sink for the doubts; a refused request is an `IncorporationError` with the Spanish
  message to show (more than the limit, a set-aside source: «La página N está apartada
  (<motivo>); recupérala antes si quieres incorporarla.»).
- `GET /api/subjects/{subject_id}/topics/{topic_id}/notes/generation` (protocol 1.6, #258) is
  removed (#440): it only polled the background generation of an end with `prepare_notes`, which
  no client asks for any more; the `NotesGenerator` no longer keeps a per-topic status.
- **The doubts API** (`server/doubts_routes.py`, `doubts_router()`), web-only, not phone
  protocol, for the pending panel (#80): thin over `editor.doubts` (`docs/modules/editor.md`),
  over the vault and `GitSync` of the `SessionService`, host `SessionService.host` for the review
  sessions. One doubts operation per topic at a time; reviewing and answering also hold the
  topic's notes lock with "prepárame el tema" (`NotesGenerator.claim`, as a `TURN_HOLDER`: they
  apply their edits under the short write lock on the latest notes, so a student save interleaves
  and the editor is re-asked on the new notes, #325) and call the `editor` role through
  `llm_transport`, bound to the topic's ledger. With an unended session of the topic that is
  active on this backend the events go to that live session through the bus
  (`DoubtChat.live`), so review, answer and dismiss work during a session (#325). An answer or a
  dismissal is broadcast on the workspace stream (`doubt.resolved`, plus `notes.changed` origin
  `editor` when the notes changed), a review's auto-resolutions as `doubts.auto_resolved`; then
  the `DoubtChat` prepares the doubts and announces the marks (`DoubtChat.schedule`,
  `doubts.marked`).
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/doubts/marks` (#516) -> `DoubtMarks`
    (`editor.doubt_marks`): `subject`, `topic`, `count` and `marks`, each `{pending_id, kind,
    text, level: block|section|top, blocks: [{section, number}], section, asked}`, in the order of
    the notes: where the web's notes viewer draws each open doubt's «?» badge. Reads only.
  - `POST .../doubts/{pending_id}/ask` (#516), no body -> `{pending_id, asked, status:
    open|auto_resolved, summary}`: shows that doubt in the workspace chat (a badge clicked, or
    «Ver la siguiente»; `DoubtChat.show`): reviewed first when it has no question yet and Claude
    is available (auto-resolved then: `asked: false`, the review's `summary`, and
    `doubts.auto_resolved` + `doubts.marked` on the stream), else asked (`ask_in_chat`) and
    announced as `doubt.asked`; the doubt last asked and still open is announced again without a
    new event. Never takes the notes lock; 404 unknown, 409 `doubt_closed`.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/doubts` -> `DoubtsQueue`: `subject`,
    `topic`, `open_count`, `current` (the id of the open doubt to ask next, `null` when none) and
    `items`, open first, each `{item, question, outcome}`: `item` is the observer's `PendingItem`
    (as in `/pending`), `question` `null` or `{pending_id, question, suggestions, options:
    [{source_id, says}], asked_at}`, `outcome` `null` or `{pending_id, status, resolution,
    evidence: [{source_id, quote}], answer, suggestion, chosen_source, discarded, keep_discarded,
    notes_changed, warning, resolved_at}`. Reads only.
  - `POST .../doubts/review`, optional body `{"confirm_over_cap": false}` -> `ReviewResult`
    (`auto_resolved`, `asked`, `notes_changed`, `summary`, `revision`, `session_id`, `commit`,
    `attempts`, `warning`, `model`): the editor auto-resolves what the sources answer (cited) and writes a question for
    every other open doubt. The web calls it after "prepárame el tema".
  - `POST .../doubts/{pending_id}/answer`, body `{"suggestion": 1}` (1-based) or
    `{"answer": "..."}` or, for a contradiction, `{"source_id": "sources/notes/page-001.jpg",
    "keep_discarded": true}` (an `answer` may go with either), plus `confirm_over_cap` ->
    `ResolutionResult` (`subject`, `topic`, `pending_id`, `status` `resolved`, `resolution`,
    `notes_changed`, `revision`, `session_id`, `commit`, `attempts`, `warning`, `model`).
  - `POST .../doubts/{pending_id}/dismiss`, no body -> `ResolutionResult` with `status`
    `dismissed`; never calls Claude, so it works without `llm_transport`.
  - Errors, Spanish `detail` plus `code` where noted: an unknown topic or doubt 404 (`"No existe
    esa duda en este tema."`); a doubt already closed (`doubt_closed`, `"Esa duda ya está
    cerrada."`), a topic with an unended session this backend cannot write to -- not the active
    one -- (`session_open`, `"Este tema tiene una sesión sin terminar: ..."`), a review before the notes exist, another doubts or notes operation of
    the topic running, or a reached cost cap (`cost_cap_reached`) until the body says
    `confirm_over_cap` 409; an answer that does not fit the question 422; no
    `llm_transport` 503 for review and answer; a Claude refusal or failure 502; a vault that
    cannot be opened 503. Needs the bearer check like every non-exempt route.
- **The editor chat API** (`server/revise_routes.py`, `revise_router()`), web-only, not phone
  protocol, for the chat beside the notes (the web client is #71): thin over `editor.revise`
  (`docs/modules/editor.md`), over the vault and `GitSync` of the `SessionService`. A turn and an
  undo hold the topic's notes lock with "prepárame el tema" and the doubts
  (`NotesGenerator.claim`; a turn and a "¿por qué?" as `TURN_HOLDER`, so turns of one topic run
  one at a time but a student save does not wait for them: the turn applies its ops under the
  short write lock of `editor.notes_lock` and, when the student saved meanwhile, re-asks the editor
  on the new notes); a turn on a topic without notes starts them from `# <topic title>`; a turn calls the `editor` role through `llm_transport`, bound to the
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
      `proposed_style_rules` (to confirm with the style guide API below), `commit`, `revision`
      (of the notes after the turn), `warning`, `errors`, ... -- or `error` `{"status": 409|502|500, "detail":
      "...", "code"?: "..."}` (a reached cost cap 409 `cost_cap_reached`, built with
      `cost_cap_error`, until the body says `confirm_over_cap`; a Claude refusal or failure 502);
      `code` is gated like a REST error body (`speaks_error_codes`); the stream then ends.
    The turn runs in its own task: a client that disconnects does not cut the change in half.
    It is also broadcast on the topic's workspace stream (below): `turn.started` (origin
    `typed`), the reply, `turn.result` or `turn.error` (with `code` always), and `notes.changed`
    (origin `editor`) when the notes changed; the `result` carries the same `turn_id`.
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
  - `GET .../notes/chat` -> `ChatHistory` (`turns`: `{time, kind, turn_id, origin,
    request_summary, transcript, message, reply, applied, summary, changed_sections, commit,
    undone, warning, refs, proposed_style_rules, feedback, crop}`, oldest first -- `kind` `explain` for a "¿Por
    qué?" answer, with its `refs`; `origin` `voice` for a turn that answered a spoken request,
    with `request_summary` (what was asked, one short line) and `transcript` (`{request_id,
    summary, session_id, segment_ids, t_start_ms, t_end_ms, text}`, the raw span), both `null`
    for a typed turn; `summary` stays the applied change's; `feedback` `{id, kind, title}` when
    the turn recorded app feedback instead of editing, #472, else `null`; `crop` the page region
    the turn cropped (`CropRef` `{source, region, source_id, path, error}`, #493: the new
    `sources/images/img-NNN.<ext>`, listed by `.../sources` like any image, or the Spanish reason
    it failed), else `null`; `can_undo`). Reads
    only; works without `llm_transport`. The live turn's result (the `RevisionResult`) carries
    the same `feedback`: a turn of the workspace chat, typed or spoken, may record a bug or an
    improvement of the app in the vault's feedback inbox (`editor.feedback`,
    `docs/modules/editor.md`) and never changes the notes then.
  - `POST .../notes/chat/undo`, no body -> `UndoResult` (`undone_commit`, `summary`, `commit`,
    `notes_changed`, `diff`, `notes`, `paths`, `revision`): reverts the latest applied turn not yet undone
    (again for the one before). No Claude call. An undo that changed the notes is a
    `notes.changed` (origin `editor`, summary `Deshecho: <summary>`) on the workspace stream.
  - Errors before the stream, Spanish `detail`: no `llm_transport` 503 (chat), a vault that cannot
    be opened 503, an unknown topic 404, no notes yet 409 for `why` only (`"Todavía no hay apuntes
    de este tema: ..."`), another notes or doubts operation of the topic running 409, an invalid body 422; undo:
    nothing to undo or a file changed after that turn 409. Needs the bearer check like every
    non-exempt route.
- **The student's document edits** (`server/notes_edit_routes.py`, `notes_edit_router()`, #313),
  web-only, thin over `editor.direct_edit` and `vault.put_pasted_image`; no Claude call, so they
  work without `llm_transport`.
  - `PUT /api/subjects/{subject_id}/topics/{topic_id}/notes`, body `{"text": "<the whole
    apuntes.md>", "base_revision": "<revision of GET .../notes>" | null}` (`null`: the topic had no
    notes) -> `StudentEditResult` (`revision`, `commit`, `diff`, `notes` -- the text as saved,
    normalised --, `changed_sections`, `normalised`, `notes_changed`). One commit per save
    (`Apuntes de <s>/<t> editados por el estudiante`), a `student_edit` record in
    `conversations/editor.jsonl`, and `notes.edited` published on the bus (persisted, origin
    `user`, payload the result without `notes`, plus `origin: "user"`) when the topic's session is
    the active one. It does not wait for a chat turn (see the editor chat API). A save that
    changed the notes is a `notes.changed` (origin `user`) on the workspace stream.
  - Errors: a stale `base_revision` 409 `{"detail", "code": "notes_changed", "text", "revision"}`
    with the current notes (`code` gated like any REST error code); `409 notes_busy`
    (`ApiError`) while anything but a chat turn holds the topic's notes lock -- "prepárame el
    tema", a restore, the doubts, an undo; text that breaks the format even after normalising 422
    `{"detail", "errors": [...]}` (Spanish validator messages); an unknown topic 404; a vault that
    cannot be opened 503.
  - `POST /api/subjects/{subject_id}/topics/{topic_id}/sources/images`: `multipart/form-data`
    with one `file` part, a PNG, JPEG or WebP image told by its bytes (not its name or part type),
    read as it streams under `[sources] max_pasted_image_bytes` (default 10 MiB) -> 201
    `PastedImage` (`source_id` `sources/images/img-NNN.<ext>`, vault-relative `path`, `markdown`
    `![Imagen pegada N](../sources/images/img-NNN.<ext>)` to insert in the notes; saving them
    then adds the image's footnote). The sync loop commits it (`SessionService.note_change`).
    Errors: too large 413; not such an image, empty, another part or a malformed body 422; an
    unknown topic 404; a vault that cannot be opened 503.
- **Spoken and typed requests become editor turns** (`server/assistant_requests.py`,
  `AssistantRequestConsumer`, #315, #327): started in the app's lifespan when the app has an
  `llm_transport`; it subscribes to `assistant.request` on the bus (`observer.AssistantRequest`,
  published by the observer's detector, the wake word or a typed message) and, per topic, runs one
  request at a time in arrival order. On arrival: `request.detected` on the workspace stream, then
  the request
  is queued (in the backend: it survives a client that goes away, and the requests of a session
  that ended meanwhile are still processed). Its turn takes the topic's notes lock
  (`NotesGenerator.claim`), waiting while a typed turn, a generation, a restore or the doubts hold
  it (at most `CLAIM_TIMEOUT_SECONDS`, 600 s, then `turn.error` 409), then dispatches on the kind
  through `HANDLERS`; the turn's `origin` is `voice`, or `typed` for a typed request (then no
  `ChatRequestRef`: a typed chat turn):
  - `edit`, `question` (app feedback, «apunta una mejora: …», classifies as `question`, #472; the
    turn is attributed to the request's `session_id`): `editor.revise_notes` on the latest notes with the request's raw `text` as
    the message and a `ChatRequestRef` as `request` (a voice chat turn in `GET .../notes/chat`);
    `editor` role bound to the topic's ledger; `notes.edited` on the bus when the session is
    still active. Both this and the typed `POST .../notes/chat` pass `revise_notes` the app's
    `settings` and a `crop_client` (`editor.crop.crop_client`, role `observer`, same transport and
    topic ledger), so a `crop_image` turn (#493) locates its region through the app's own
    transport, never a default one.
  - `prepare_notes`: "prepárame el tema" through `NotesGenerator.generate` (the batched mode of
    #326 by default: each batch an `incorporate` turn), the same generation as the button, never
    past a reached cap unconfirmed: that is `turn.error` 409 with code `cost_cap_reached` (the
    student confirms it like any other request, below), never a silent spend.
  - `incorporate`: `NotesGenerator.incorporate(targets)` (#326), a turn of kind `incorporate`; a
    refused incorporation (a set-aside page, too many sources, an unknown one) is `turn.error` 422
    with the editor's Spanish message.
  - `set_aside` / `restore`: `sources.set_capture_triage` for each target not already in that
    state, each change a `capture.triaged` (origin `user`) in the topic's live session -- the
    transcriber then transcribes a restored page with no transcription -- or else in a review
    session; one short Spanish chat entry is streamed as the reply («He apartado la página 3.»,
    «He recuperado la página 3; se está transcribiendo.» / «...; se transcribirá en la próxima
    sesión del tema.», «La página 1 no estaba apartada.») and recorded as a `triage` chat turn
    (`editor.record_triage_turn`, `ChatTurn.kind` `triage`); `notes.changed` untouched;
    `turn.result` is the `TriageTurn`. An unknown target is `turn.error` 404. A `set_aside`
    (#351) also says why: `TriageTurn.targets` (and the history's `ChatTurn.targets`) lists every
    target -- set aside now, or `already` before -- with the reasons its capture triage gives
    (`reasons`, `sources.triage.TriageReason` codes, and `duplicate_of`; empty for a page triage
    kept), and the reply names them in brackets («He apartado la página 9 (página en blanco) y
    la página 4.», «La página 1 (repetida de la página 2) ya estaba apartada.»). A flagged page
    set aside keeps its one reason in its sidecar (`set_capture_triage(reason=...)`). A `restore`
    carries no targets; a set-aside with no reason reads as before.
  - `doubt_answer`: `editor.answer_doubt(pending_id, answer)` (#325; digits pick that suggestion,
    anything else is the answer's words) under the notes lock, the resolution streamed as the
    reply, then `doubt.resolved` (+ `notes.changed` when the notes changed) and the next doubt
    through the `DoubtChat`; `turn.result` is the `ResolutionResult`. A closed or unknown doubt is
    `turn.error` (409 `doubt_closed`, 404).
  - `study` (#335, "ya está, quiero estudiar"): `study_routes.switch_to_study`, the same service
    as `POST .../study` (below), under the consumer's notes lock: the topic's capture session is
    ended (reason `command`) without "prepárame el tema" and the notes are labelled "versión de
    estudio". One line is streamed as the reply («He cerrado la captura y marcado los apuntes v3
    como versión de estudio.»; «He marcado ...» when no session was open) and `turn.result` is the
    `StudyTurn` (`turn_id`, `origin`, `request`, `message`, `reply`, `action: {kind: "go_study",
    path: "/subjects/<s>/topics/<t>/study"}`, `study`: the `StudyState`), which the web shows as
    one "Ir a Estudiar" button (#337). Not recorded in the chat history. No notes is a
    `turn.error` 409 with a Spanish `detail`.
  A failure is a `turn.error` with the status the same failure has over REST; the topic's next
  request runs anyway. A request stopped at the cost cap (`turn.error` `cost_cap_reached`) is kept
  in memory (the last `STOPPED_PER_TOPIC`, 32, of each topic, by the failed turn's `turn_id`) for
  a confirmation (#351): `AssistantRequestConsumer.confirm(s, t, turn_id)` queues the same,
  already classified request again with `QueuedRequest.confirm_over_cap`, threaded to
  `revise_notes`, `NotesGenerator.generate`, `NotesGenerator.incorporate` and `answer_doubt`; it
  is not classified or announced again (no second `request.detected`) and its new turn carries
  the same `request_id`. Confirmed once; nothing is kept across a restart (`NotStoppedError`).
  The reader catches and logs an exception per event, so one bad event never ends it; a request
  kind with no handler (`HANDLERS`, which covers every `observer.REQUEST_KINDS`) is a
  `turn.error` 422 with a Spanish `detail` (`UNKNOWN_KIND_DETAIL`).
- **Requests survive a restart** (#408): every request whose turn ended in a result or an error
  (a busy topic, an unknown kind and a reached cap included) is recorded as a persisted
  `turn.finished` event (`TURN_FINISHED_KIND`, origin `editor`, `{request_id, session_id,
  turn_id, kind, outcome: "result" | "error", status?, code?}`) in the request's session while
  it is attached
  (written before `turn.result` is announced). On every `session.started` / `session.resumed` on
  the bus, and at `start()` for a session already attached, `AssistantRequestConsumer.replay`
  reads the session's `events.jsonl` and queues again through `submit`, oldest first,
  every `assistant.request` no turn answered (`unanswered_requests(events, session_id, turns)`):
  no `turn.finished` of its id, no voice chat turn (`editor.chat_history`) whose `transcript` is
  that request of that session, and after the session's newest answered request (a topic's
  requests run in order, so what precedes an answered one ran; this also keeps a session left
  open across the upgrade from replaying requests answered before `turn.finished` existed). A
  replayed request is announced (`request.detected`) and streams as usual. `submit` takes each
  `(session_id, request_id)` once per process, so a request queued, running or replayed already
  is never queued twice (a reconnect's resume replays nothing). Shutdown gives running turns 5 s,
  then cancels them; the cancelled turns and the still-queued requests have no `turn.finished`,
  so they run when the session is resumed on the restarted backend (the log names them).
- **Requests of an ended session survive a restart** (#423): a typed request kept in a review
  session (no capture session was open) and a request whose session ended before its turn ran
  are recorded as outstanding, in a `requests.outstanding` event (`REQUESTS_OUTSTANDING_KIND`,
  origin `editor`, `{session_id?, request_ids}`; no `session_id`: the event's own session) of a
  review session: `post_message` writes it in the review session of its typed requests, and a
  `session.ended` on the bus writes it, in a new review session, for the ended session's
  requests still queued or running (a `study` request ending its own session included). A
  `turn.finished` (which now always carries the request's `session_id`) whose session is no
  longer attached is written in a review session. Once the vault is open (`catch_up_vault`, a
  `SessionService.add_on_open` hook, so after the restarted backend's first request that opens
  it), every readable topic's `outstanding_requests(vault, subject, topic)` -- each named request
  with no `turn.finished` of that session's request anywhere in the topic and no voice chat turn
  of it -- is submitted again through `submit`, oldest first, announced and streamed as usual,
  and answered exactly once (the `turn.finished` is written before `turn.result`). This reads
  every event log of the vault once at start. Review sessions written before #423 hold no
  `requests.outstanding`, so nothing older is run again. A named request whose
  `assistant.request` cannot be read is left out with a warning.
  The request classifiers' context (`sources_lookup(sessions)`, `request_context`) is wired here
  into the detector and the typed classifier: the topic's `editor.source_status` rows and the
  open doubt last asked in the chat (`editor.doubts.doubt_chat_turns`).
- **Typed workspace messages** (`server/workspace_routes.py`, #327):
  `POST /api/subjects/{subject_id}/topics/{topic_id}/workspace/messages` `{text,
  selected_source_ids?}` (1..4000 characters) -> 202 `{message_id, requests: [AssistantRequest], classified}`. The text is
  classified by `observer.requests.MessageClassifier` (`app.state.message_classifier`, built with
  an `llm_transport` whatever `[observer] request_detection` or `enabled` say) and each request is
  persisted as `assistant.request` (origin `user`, `detector: "typed"`, id `req-t<n>`,
  `message_id`) in the topic's live session -- the bus brings it to the consumer -- or in a review
  session started and ended for it, then queued (`AssistantRequestConsumer.post_message`). A
  message with no request, or a classifier failure (`classified: false`), becomes one `edit` (a
  `question` when it has a `?`) with the raw text, so nothing typed is lost. Errors: an empty text
  422, an unknown topic 404, a vault that cannot be opened 503, no `llm_transport` 503.
  `{confirm_over_cap: true, turn_id}` instead of `text` (#351) is "Continuar igualmente" on a turn
  stopped at the cost cap: `AssistantRequestConsumer.confirm` (above), answered 202 with the one
  request (its `message_id`, or a new one for a spoken request); a `turn_id` not waiting for a
  confirmation 404 (Spanish detail), a `turn_id` with `text` or without `confirm_over_cap` 422.
  `POST .../notes/chat` (and its own `confirm_over_cap`) stays for the old notes page.
  **The Recursos selection** (#433): `selected_source_ids` (optional) is what the student has
  selected in the Recursos tab (#432), in order: topic-relative source ids
  (`sources/notes/page-003.jpg`; a PDF page as `<pdf>#page=K`, accepted and kept), at most
  `[observer] max_selected_sources` (default 20), repeats dropped. It is checked by
  `assistant_requests.check_selection` against `editor.source_status` (set-aside sources count):
  too many ids, an id that is not a source of the topic, or a fragment other than a PDF's
  `#page=K` is a 422 with a Spanish `detail`; with `confirm_over_cap` it is a 422. It goes to
  `MessageClassifier.classify(selected=...)` (the referent of «esto», «estas páginas»; nothing
  selected and nothing named: a `question` asking which pages), is kept on every request of the
  message (`AssistantRequest.selected_source_ids`, persisted, so a confirmation past the cap and
  a replay after a restart keep it) and echoed by `request.detected`. An `edit`/`question` turn
  passes it to `editor.revise_notes(selected_sources=...)` (an empty list for a typed message
  with nothing selected, so the editor asks instead of guessing; `None` for a spoken request).
  An `incorporate` uses its `targets` as before. A `set_aside`/`restore` whose targets hold a
  selected source that is not a captured page (a PDF, a web page) leaves it alone and says so in
  the reply («… no se puede apartar: solo se apartan y recuperan páginas capturadas (apuntes o
  libro).»), the rest applied. A body without the field is accepted as before; the classifier is
  then told nothing is selected.
- **The workspace stream** (`server/workspace.py` `WorkspaceHub`, `server/workspace_routes.py`,
  #315), for the study workspace's chat and document:
  `GET /api/subjects/{subject_id}/topics/{topic_id}/workspace/stream` -> `text/event-stream`
  (`Cache-Control: no-cache`), read with `fetch` like the chat streams. It starts with a
  `: connected` comment and sends a `: keep-alive` comment every 15 s without events; it ends when
  the client goes away or the app shuts down. It works with or without an active session and
  without `llm_transport`. Fed by an in-memory per-topic hub (`app.state.workspace`) that voice
  turns, typed turns, student saves, generations, restores and undos publish to; nothing is
  persisted or replayed (a reconnecting client reloads `GET .../notes/chat` and `GET .../notes`).
  Each event is `event: <name>` plus one line of JSON (the list is open; later tasks add kinds):
  - `request.detected` `{request_id, kind, summary, origin: voice|typed, transcript: {session_id,
    segment_ids, t_start_ms, t_end_ms, text}, targets?, selected_source_ids?}`: a spoken or typed
    request was queued (`selected_source_ids`: the Recursos selection its typed message carried,
    #433);
  - `turn.started` `{turn_id, request_id|null, origin: typed|voice, kind:
    revise|prepare_notes|incorporate|set_aside|restore|doubt_answer|study}` (`incorporate`: one
    incorporation, e.g. each batch of a batched "prepárame el tema", #326);
  - `reply.delta` `{turn_id, text, attempt}`, `reply.restart` `{turn_id, attempt}`;
  - `turn.result`: the `RevisionResult` (for `prepare_notes` the `GenerationResult`, for
    `incorporate` the `IncorporationResult`, for `set_aside`/`restore` the `TriageTurn`, for
    `doubt_answer` the `ResolutionResult`, for `study` the `StudyTurn`) plus `turn_id`,
    `request_id` and `kind`;
  - `turn.error` `{turn_id, request_id, status, detail, code?}`;
  - `notes.changed` `{revision, origin: editor|user|generation|restore, summary, turn_id?}`: the
    notes changed (a chat turn or an undo: `editor`; a student save: `user`; a generation that
    wrote the notes, whichever way it started: `generation`; a restore: `restore`; a doubt's
    answer or a review's auto-resolution: `editor`);
  - `doubt.asked` `{pending_id, kind, text, question, suggestions, options: [{source_id, says}],
    refs}`: the chat shows one open doubt (#325, #516: when the student opens it; `text` is its
    explanation, `refs` the source ids it is about);
  - `doubt.resolved` `{pending_id, status: resolved|dismissed, resolution, notes_changed}`: a doubt
    was answered or dismissed (`POST .../doubts/{id}/answer|dismiss`);
  - `doubts.auto_resolved` `{pending_ids, summary}`: the editor settled those doubts from the
    sources itself; `summary` is the one short chat line;
  - `doubts.marked` `{count}` (#516): how many open doubts the notes mark now (after the
    `DoubtChat` prepared them); the web reads `GET .../doubts/marks` again;
  - `incorporation.progress` `{done, total, source_ids}`: a batched "prepárame el tema" finished
    the batch `source_ids`; `done` of the `total` pending sources are incorporated (#326);
  - `study.marked` `{version, tag}`: the topic switched to Estudiar and notes version `version`
    is labelled "versión de estudio" (`POST .../study` or the chat's `study` request, #335).
  A slow subscriber never blocks a publisher: past 1024 queued events the oldest `reply.delta`
  (else the oldest event) is dropped. Errors before the stream: an unknown topic 404, a vault
  that cannot be opened 503. Needs the bearer check like every non-exempt route.
- **Doubts in the workspace chat** (`server/doubt_chat.py`, `DoubtChat`, `app.state.doubt_chat`,
  #325, #516): the doubts are never written into the notes and, since #516, **not asked one after
  another**: the web marks them in the notes viewer (`GET .../doubts/marks`) and the student opens
  one (`POST .../doubts/{id}/ask`, `DoubtChat.show`). After every editor write that changed the
  notes or raised doubts -- a typed turn (`revise_routes.py`), a spoken one
  (`assistant_requests.py`), "prepárame el tema" (`NotesGenerator`), a doubt answered, dismissed
  or reviewed (`doubts_routes.py`) -- the topic is `schedule`d and a preparer task runs (one per
  topic; a schedule meanwhile runs it once more). It does not take the notes lock, so the
  student's next turn is never refused because of it, and it waits for nothing while "prepárame
  el tema" rewrites the notes (the generation schedules it again). Without notes it does nothing
  more; otherwise the relevant open doubts without a question (`editor.doubts.ask_plan`, at most
  `REVIEW_BATCH`, 5) are reviewed by the editor (`review_doubts(pending_ids=...)`: those the
  sources settle are auto-resolved, one `doubts.auto_resolved` line; the others get their
  question, ready for when the student opens them), then `doubts.marked` `{count}` is announced.
  Items whose pages are all set aside are never marked. A failed review (a reached cap, a Claude
  failure) is logged; such a doubt is shown with a generic question. `show` reviews a doubt
  without a question first (it may be auto-resolved instead of asked), then writes its question
  with `in_chat: true` (`ask_in_chat`) and announces `doubt.asked`. Its events go to the topic's
  live session when it is active (`DoubtChat.live`, a `LiveSink` publishing on the bus with the
  given origin), else to a review session. The chat's history (`GET .../notes/chat`) shows the
  asked doubts as turns of kind `doubt` (with `doubt_text`, their explanation; one turn per doubt,
  at its latest asking) and the auto-resolutions as `doubts_resolved`, and incorporations as
  turns of kind `incorporate` (`source_ids`, `diff`). `[editor] doubts_in_chat = false` turns the
  preparer off (the doubts API, the marks and the announcements of its routes stay). A chat
  message ("la segunda", "pone «escrita»") is routed to the doubt last asked and still open (#327).
- **The voice tutor API** (`server/tutor_routes.py`, `tutor_router()`, #82): thin over
  `editor.tutor` (`docs/modules/editor.md`), over the vault and `GitSync` of the
  `SessionService`; the `editor` role through `llm_transport`, bound to the topic's ledger. It only
  reads the notes, so it does **not** take the notes lock (it answers while "prepárame el tema" or
  a chat turn runs); one question per topic runs at a time (its own lock). Used by the web capture
  page's tutor and the Android app's (#248) with the default `style: "spoken"` (role `editor`),
  and by the study screen's question chat (#336) with `style: "written"` (#334), whose role is
  `[editor] study_chat_role` (`SA_EDITOR__STUDY_CHAT_ROLE`: `editor`, by default, or
  `observer`, Sonnet, to compare; the ledger records the role used).
  - `POST /api/subjects/{subject_id}/topics/{topic_id}/tutor`, body `{"question": "¿Qué era la
    derivada?", "style": "spoken", "confirm_over_cap": false}` (`question` 1-1000 characters;
    `style` `spoken` | `written`, default `spoken`, anything else 422) -> the same
    Server-Sent Events stream as `notes/why`: `reply.delta` `{"text", "attempt": 1}`, then
    `result` -- the `TutorAnswer`: `style`, `question`, `reply` (with the notes' `[^label]` marks
    and, written, `[§anchor]` marks), `refs` (`{label, kind, text, source_id, path}` per notes
    footnote the answer cites, in order), `sections` (`{anchor, title}` per section of the current
    notes a written answer cites, in order; always empty when spoken), `warning` (also naming cited
    anchors the notes lack), `model` -- or `error` `{"status", "detail", "code"?}` (a reached cost cap
    409 `cost_cap_reached` until `confirm_over_cap`; a Claude refusal or failure 502). The
    question runs in its own task; the answer is appended to `conversations/tutor.jsonl`.
  - **Generation requests** (#366, `server/study_requests.py`): a `written` question is first
    matched, deterministically (no LLM call), by `match_generation(text, *, registry) ->
    GenerationRequest | None` against a small Spanish grammar: a verb (`hazme`, `haz`,
    `genera(me)`, `prepára(me)`, `crea(me)`, `quiero`, `dame`; accents and a separate `me`
    optional; `otra vez` / `de nuevo` ignored) then one material: `esquema` -> kind `esquema`;
    `ejercicios` -> `examen` (option `ejercicios`); `examen` / `simulacro` -> `examen` (option
    `examen`); `quiz` / `test` / `preguntas` -> `quiz`; `tarjetas (de memoria)` / `flashcards` ->
    `flashcards` (option `tarjetas`); `diapositivas` -> `diapositivas` (kinds from the generator
    modules' `KIND`; a kind the registry lacks is no match). A count (digits or Spanish words:
    "de 10 preguntas", "20 tarjetas", "5 ejercicios", "un examen de 4 preguntas", "un quiz de
    10") sets `QuizOptions.size`, `FlashcardsOptions.size`, `ExamOptions.exercises` /
    `questions` or `SlidesOptions.size`, clamped to the bounds of the registry's options model
    (the lines say so: "Como mucho pueden ser 30 preguntas: preparo 30."); `fácil(es)`,
    `media(s)`, `difícil(es)` set `QuizOptions.difficulty`. A request that sets none of its
    option's parameters asks back first (below); the progress line states the effective options,
    defaults included. Anything else -- "¿qué es un quiz?", "explícame
    el esquema de la página 3" -- goes to the tutor as before, and `spoken` questions are never
    matched. A match runs `generators_routes.generate_material` -- the same code path as
    `POST .../generated/{kind}` (the `MaterialGenerators` claim of the topic and kind, a
    `generator` client on the topic's ledger, `[generators] grounding_min_support`) with the
    body's `confirm_over_cap` -- and not the tutor's lock. Its stream, instead of the tutor's:
    - `generation.started` `{kind, option, text}`: `option` the study option key (`esquema`,
      `ejercicios` or `examen` for kind `examen`, `quiz`, `tarjetas`, `diapositivas`), `text` a
      Spanish line, e.g. "Preparando un quiz de 10 preguntas de dificultad variada con tus
      apuntes v4…" (the newest `apuntes-vN`, none when untagged);
    - then `result` `{kind: "generation", option, material_kind, question, reply, items,
      warnings, study}`: `reply` e.g. "Listo: 10 preguntas. Ábrelo en «Quiz»." (slides, #380: "Listas:
      9 diapositivas. Ábrelas en «Diapositivas»."), `items` the material's item
      count, `warnings` the generation's (Spanish), `study` the fresh `StudyState` of
      `GET .../study`;
    - or `error` `{status, detail, code?}` as the tutor's: a reached cap 409 `cost_cap_reached`
      ("Confirma para generar el material igualmente."), the same material being generated 409,
      no notes 409, a Claude refusal or failure 502. For a match these are all in the stream
      (after `generation.started`), not before it.
    The turn is appended to `conversations/tutor.jsonl` (`editor.tutor.record_generation`, a
    `tutor.generation` record); a failed generation is not saved.
  - **Asking back** (#383, human decision 2026-09-27): a **bare** match -- one that sets none of
    the count or difficulty its option asks for (`needs_parameters(request, *, registry) ->
    Clarification | None`) -- generates nothing yet. `quiz` asks for `size` and `difficulty` (it
    asks only when neither is given: "hazme un quiz difícil" generates 10 hard questions);
    `tarjetas` for `size`, `ejercicios` for `exercises`, `examen` for `questions`,
    `diapositivas` for `size`; `esquema` has no options and never asks. The defaults it names come
    from the registry's options models. Unless the topic has no notes (then the generation stream
    reports it, as before), the stream is one `result` `{kind: "clarification", option, style:
    "written", question, reply, refs: [], sections: [], warning: null, defaults}` -- answer-shaped,
    so a client that knows only answers shows `reply` -- e.g. «¿Cuántas preguntas quieres y de qué
    dificultad (fácil, media, difícil o variada)? Si no me dices nada distinto, hago 10 preguntas
    de dificultad variada.» or «¿Cuántas tarjetas quieres? Por defecto, 20.». It is appended as a
    `tutor.clarification` record (`editor.tutor.record_clarification`: `question`, `reply`,
    `option`, `material_kind`, `defaults`). While that is the topic's latest tutor turn
    (`editor.tutor.pending_clarification`), the next `written` message is dispatched: a new
    request ("hazme un esquema") is handled as that request; otherwise
    `complete_parameters(text, pending, *, registry) -> GenerationRequest | None` reads it -- a
    count and/or a difficulty in any order ("5", "10 fáciles", "difícil, 8", "de 12", "6
    ejercicios y 3 preguntas"; `variada`/`mixta` too), or an acceptance of the defaults ("vale",
    "sí", "las de por defecto", "como quieras", "da igual") -- and a completion runs the
    generation stream above with the pending option (missing parameters at their defaults, counts
    clamped and said so). Anything else goes to the tutor as a question, whose answer turn drops
    the clarification (a later "10" is then a question too). The Construir chat, `spoken`
    questions and `POST .../generated/{kind}` never ask back.
  - `GET .../tutor` -> `TutorHistory` (`turns`: `{time, kind, style, question, reply, refs,
    sections, warning, option, items, feedback}`, oldest first, both styles; `feedback` the app
    feedback a written `answer` turn recorded, #472, as in the result; `kind` `answer` (the default:
    turns recorded before #366 read so), `generation` (with `option` and `items`, `reply` the
    result sentence, `warning` its warnings joined) or `clarification` (#383, with `option`,
    `reply` the question back); turns recorded before #334 are `spoken`
    with no `sections`). Reads only; works without `llm_transport`.
  - Errors before the stream, Spanish `detail`: no `llm_transport` 503, a vault that cannot be
    opened 503, an unknown topic 404, no notes yet 409, another question of the topic running
    409, an empty or too long question 422. Needs the bearer check like every non-exempt route.
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
    `version` and `tag` of the new `apuntes-vN`, `commit`, `diff`, `notes`, `revision`, `errors`,
    `warning`).
    Holds the topic's notes lock (`NotesGenerator.claim`, when the app has one); `notes.restored`
    is published on the bus (origin `editor`) when the topic's session is the active one, and
    `notes.changed` (origin `restore`) on the workspace stream.
  - Errors, Spanish `detail`: a vault that cannot be opened 503, an unknown topic or version 404,
    a version below 1 or a diff without `from` 422, no current notes to diff with, the current
    notes already being that version, or another notes operation of the topic running 409.
  - `NotesVersions` also carries `study` per version, `study_version` and `study_current` (#335).
- **Switch to Estudiar** (`server/study_routes.py`, `study_router()`, #335): no Claude call, so
  both routes work without `llm_transport`.
  - `POST /api/subjects/{subject_id}/topics/{topic_id}/study`, no body -> 200 `StudyState`.
    `switch_to_study` ends the topic's unended capture session if it has one
    (`SessionService.open_session_of`, then `SessionService.end` as the capture page's end does:
    end hooks, checkpoint and push) but **without** starting "prepárame el tema", then
    `editor.mark_study_version` labels the notes (the latest `apuntes-vN` when the notes equal
    it, else a new tag), recorded in `study/version.yaml`. A label, not a freeze. `study.marked`
    `{version, tag}` goes on the workspace stream. Holds the topic's notes lock
    (`NotesGenerator.claim` as a `TURN_HOLDER`, when the app has one) while it works. The answer
    adds `created_tag` and `ended_session` (the session it ended, or null). Errors: `409
    notes_busy` while the notes are being prepared or an editor turn runs, 409 with a Spanish
    `detail` when the topic has no notes (nothing is ended then), 404 unknown topic, 503 vault.
  - `GET .../study` -> `StudyState` (reads only): `study_version` (`{version, tag, marked_at}` or
    null), `study_current` (the notes still equal the label) and `options`, one per study option
    `{key: esquema|ejercicios|examen|quiz|tarjetas|diapositivas, kind, state:
    listo|desactualizado|sin_generar, stale_reason, notes_version}`, six in that order, from
    `generators.materials_status` (ejercicios and examen both from kind `examen`, tarjetas from
    `flashcards`, diapositivas from the slides generator's kind `diapositivas`, #380;
    `notes_version` the `apuntes-vN` the material was built from). Staleness is the generators'
    own: an edit after a material was built makes it `desactualizado`. 404 unknown topic, 503
    vault. Every response embedding a `StudyState` (`POST .../study`, the study chat's generation
    `result.study`, the `study` turn) carries the same six options. The slides' downloads need
    nothing more in the study state: `GET .../topics/{t}/generated` lists the `diapositivas`
    artifact's `files` (vault-relative: `generated/diapositivas.md` always, plus
    `diapositivas.pdf` and `diapositivas.pptx` when Marp exported them) and
    `GET .../generated/files/{name}` (`name` relative to `generated/`) serves each as a download
    -- the calls `web/src/materials/` already makes (`fetchMaterials`, `fileUrl`).
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
- **Study materials** (`server/generators_routes.py`): thin over `studentassistant.generators`
  (`docs/modules/generators.md`), over the vault and `GitSync` of the `SessionService`; the
  registry is `app.state.generators` (`generators.default_registry`).
  - `GET /api/generators` -> `[GeneratorInfo]` (`kind`, `title`, `description`, `version`,
    `options_schema`: the JSON Schema of its options).
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/generated` -> `MaterialsStatus`
    (`has_notes`, `notes_sha256`, `artifacts`: per kind `generated`, `stale`, `stale_reason`,
    `files`, `meta`). No Claude call: works without `llm_transport`.
  - `POST .../generated/{kind}`, optional body `{"options": {...}, "confirm_over_cap": false}` ->
    `GenerateResult`. Needs `llm_transport` (503 otherwise; `MaterialGenerators`, one run per
    topic and kind); a `generator` client bound to the topic's ledger. The run itself is
    `generate_material(service, vault, sync, subject, topic, kind, *, registry, options,
    confirm_over_cap)`, which the study chat's generation requests share (#366).
  - `GET .../generated/files/{name}` -> the bytes of `generated/<name>` (subdirectories allowed)
    as a download: `Content-Disposition: attachment; filename="<topic>-<file>"`, the media type
    by extension (`.apkg` octet-stream, `.csv` `text/csv; charset=utf-8`...); 404 when there is
    no such file or the name is not one (`vault.read_generated`). The web's download links.
  - Errors, Spanish `detail`: an unknown topic or kind 404, invalid options 422, no notes yet or
    the same generation running 409, a reached cap 409 `cost_cap_reached` until
    `confirm_over_cap`, a Claude failure or refusal 502, a vault that cannot be opened 503.
- **Quiz** (`server/quiz_routes.py`, #75): thin over `studentassistant.generators.quiz`;
  generating it is `POST .../generated/quiz` above.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/quiz` -> `StoredQuiz` (`quiz`, `built_at`,
    `notes_version`, `warnings`, `stale`, `stale_reason`); 404 when there is no quiz yet.
  - `POST .../quiz/results`, body `QuizAttempt` (`built_at`, `answers`: `question`, `given`,
    `self_assessed`; `duration_seconds`; optional `questions`: the ids asked in a partial
    attempt, #282) -> `QuizResult` (`questions` set when partial), graded, appended to
    `study/quiz-results.jsonl` and committed. 404 no quiz, 409 the quiz was generated again
    (`built_at` differs), 422 an answer or asked id unknown, one twice, or an answer to a
    question not asked.
  - `GET .../quiz/results` -> `[QuizResult]`, oldest first.
- **Exam correction** (`server/exam_routes.py`, #283): thin over
  `studentassistant.generators.exam_results`; generating the exam is `POST .../generated/examen`.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/exam` -> `StoredExam` (`exam` from
    `examen.yaml`, `built_at`, `notes_version`, `warnings`, `stale`, `stale_reason`); 404 when
    there is no exam to correct yet.
  - `POST .../exam/results`, body `ExamAttempt` (`built_at`, `questions`: `question`, `awarded`
    per criterion) -> `ExamResult` (score, total, percentage computed by the backend), appended to
    `study/exam-results.jsonl` and committed. 404 no exam, 409 the exam was generated again
    (`built_at` differs), 422 an unknown question, one twice or points out of range.
  - `GET .../exam/results` -> `[ExamResult]`, oldest first.
- **Practice** (`server/practice_routes.py`, #81): thin over `studentassistant.generators.practice`.
  - `GET /api/subjects/{subject_id}/topics/{topic_id}/practice[?new_limit=n]` -> `PracticeQueue`
    (`now`, `queue` of `{item, state}`, `counts`, `next_due`, `suspended`, `warnings`);
    `new_limit` 0-100, default 10 new items a day. 500 when a material cannot be read.
  - `POST .../practice/reviews`, body `PracticeAnswer` (`item`, `rating`, `given`,
    `self_assessed`) -> `ReviewOutcome` (`review`, `state`), appended to `study/practice.jsonl`
    and committed with the sitting's batch (`note_change()`). 404 an item no longer in the
    material, 422 a flashcard review without a rating.
  - `POST .../practice/items/{key}/suspend` and `POST .../practice/items/{key}/restore` (#281)
    -> `SuspensionOutcome` (`item`, `suspended`, `suspended_at`, `changed`): set the item aside
    (never queued) or bring it back with its history; the record goes to `study/practice.jsonl`
    and is committed with the sitting's batch (`note_change()`). Idempotent (a repeat writes
    nothing, `changed: false`). `key` must look like `flashcards:<id>` or `quiz:<hash>` (else
    422); 404 an item no longer in the material or an unknown topic. The items set aside come
    in the queue response (`suspended`).
- **Practice summary** (`server/practice_summary_routes.py`, #280): thin over
  `generators.practice.practice_summary`, run in a worker thread.
  - `GET /api/practice/summary[?new_limit=n]` -> `PracticeSummary` (`now`, `topics`, `totals`,
    `warnings`): per topic with practice material (`subject_id`, `subject_name`, `topic_id`,
    `topic_title`, `due`, `new`, `next_due`, what `practice_queue` offers now with `new_limit`,
    0-100, default 10; suspended items count as neither), the most due first (then most new);
    `totals` sums `due`/`new` and counts `topics`. Topics with neither flashcards nor a quiz are
    omitted; a subject or topic that cannot be read is skipped and named in `warnings` (Spanish),
    never failing the call. 503 when the vault cannot be opened. Bearer auth like every `/api`.
- `GET /api/feedback[?status=nuevo|triado|descartado]` (`server/feedback_routes.py`,
  `feedback_router()`, #472), web-only, not phone protocol -> `FeedbackList` (`items`: the vault's
  feedback inbox folded, `vault.list_feedback`, oldest first, each `{id, created_at, kind, title,
  body, context, status, issue, updated_at}`; only those in `status` when given, another value
  422). Read only: triage is the CLI's (`studentassistant feedback mark`); the backend never
  calls GitHub. A vault that cannot be opened 503, an unreadable inbox 500 (Spanish `detail`).
  Bearer auth like every `/api` route.
- **Error bodies** (`server/errors.py`, protocol 1.2, `protocol/README.md` "REST errors"): every
  REST error is `{"detail": "<Spanish>"}`; the refusals a client branches on also carry `code`
  (`studentassistant.protocol.ErrorCode`: `cost_cap_reached`, `doubt_closed`, `session_open`, and
  since 1.8 `user_required` and `user_not_found`).
  A route raises `ApiError(status, detail, code, headers=None)` (an `HTTPException`) or one of the
  helpers: `cost_cap_error(error, then)` (409 `cost_cap_reached` from a
  `CostConfirmationRequiredError`; `then` ends the Spanish sentence, e.g. `"Confirma para
  continuar igualmente."`), `user_required_error()` (400 `user_required`, #549) and
  `unknown_user_error(user_id)` (404 `user_not_found`, the id in the Spanish sentence because it is
  the one thing that says which selection went stale). The handler `install_error_handler(app)`
  puts in `create_app` answers it, adding `code` only when the caller speaks **that** code:
  `error_body` asks `caller_speaks_this_error_code(request, code)`, which reads
  `protocol.ERROR_CODES_SINCE[code]` -- so a device paired as 1.0/1.1 keeps the plain body for every
  code, and one paired as 1.2-1.7 gets the other three but not the two user codes, which are new in
  1.8 (`USER_ERROR_CODES_SINCE`) and reach it as a Spanish `detail` and a status alone, which is
  what a client that has never heard of a code does anyway (`speaks_error_codes` still answers the
  coarser "codes at all" question). Every new coded refusal (e.g. the
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
   Without that header the token is also taken from the `sa_token` cookie (`auth.TOKEN_COOKIE`,
   #83): the Android app sets it in its WebView's cookie jar to show the web UI on the phone,
   since a page cannot put a header on its own loads and `fetch` calls. A present bearer header
   wins (a wrong one is 401 even with a valid cookie). The cookie is ambient, so against CSRF a
   request authenticated **only** by it (no bearer header; a device token, not the loopback
   trust) whose method is not GET/HEAD/OPTIONS must come from this backend's own pages
   (`auth.same_site_origin`): its `Origin` -- or, without one, its `Referer` -- must name the
   same host as the request's `Host`, and that host must be one the Host allowlist accepts
   (`network.allowed_host_names` or a loopback/private literal). A missing, `null` or other
   origin gets 403. Requests with a bearer header are not checked. A loopback client passes without a token while `server.trust_localhost` is true, so the web UI
   works on the PC itself. This includes the static web app: a browser on another machine gets
   401 for `/`. Whoever passed is in `request.state.principal` (`Principal(device_id, local, protocol_version)`: the version the device sent at pairing, this backend's own for the PC).

**WebSocket routes** are not covered by the bearer middleware. Each one calls
`await authenticate_websocket(websocket)` (`server/auth.py`) before `accept()`. It reads the token
from `Authorization: Bearer <token>`, the `?token=` query parameter (browsers cannot set
WebSocket headers) or the `sa_token` cookie -- a handshake authenticated only by the cookie
must also pass `same_site_origin` (else 1008), against cross-site WebSocket hijacking --, applies the same loopback trust, and returns the `Principal`. If
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
never echoes the request's `input` back. The user selection is not in that list and is not a
secret: `sa_user` is too short for the token pattern (16+ characters after `sa_`), so the logs keep
which student a request was about while still redacting the `sa_token` cookie it travels with.

### Active user -- `server/user_scope.py` (protocol 1.8, #549)

One backend and one vault serve several students, each with their own `users/<id>/` folder
(`docs/modules/vault.md`, "Users"), and nothing but the request itself says who is calling.

- `resolve_user_id(headers, cookies, vault) -> str` is the rule of `protocol/README.md` "Users
  (1.8)" and nothing else: the `X-SA-User` header (`protocol.USER_HEADER`, what the native Android
  app sends), else the `sa_user` cookie (`protocol.USER_COOKIE`, what the web page and the Android
  WebView set), else -- when the vault holds exactly one user -- that one, so a client that knows
  nothing about users keeps working on a single-user vault. A present header wins over the cookie,
  and a value that is there but blank names nobody, so the cookie still gets its turn; header names
  are matched case-insensitively, because that is how they travel. With neither and any number of
  users but one there is nothing to assume: `UserRequiredError`. An id this vault does not have:
  `UnknownUserError(user_id)`. It reads the file system -- one listing of `users/`, no JSON read per
  request, which is `vault.user_ids`' own cheap rule and counts a folder as a user whether or not
  its `profile.json` reads back -- so a caller on the event loop runs it in a worker thread.
- `resolve_websocket_user(websocket, vault)` is the same rule on the upgrade request's headers and
  cookies. A handshake carries no REST body, so the two refusals travel as the exceptions they are
  and the gateway closes the socket on them (`ws.py`, "Session WebSocket").
- `active_user_vault(request) -> (user_vault, user_sync)` is the FastAPI dependency every
  user-scoped route declares: `vault.for_user(id)` and `sync.for_user(id)`, both views of what
  `SessionService.open_vault()` has open, so a route keeps calling the readers and writers it
  already calls and nothing it writes can land in another user's folder. The resolution and the two
  handles are built in one `asyncio.to_thread` step. A vault that cannot be opened at all is 503
  (`"No se puede abrir la bóveda."`); the two refusals become `user_required_error()` (400) and
  `unknown_user_error(id)` (404); and a folder that `user_ids` counted but whose `profile.json`
  will not open a handle (`vault.UserNotFoundError` out of `for_user`) is told to the student as
  that same 404 `user_not_found`.

**The selection is not authentication.** Neither the header nor the cookie is a credential: they
name a user, they do not prove one, and whoever can reach the backend can name any user of it --
the bearer/loopback trust of ADR-0001 is what guards the door, unchanged, and a device pairs with
the backend, never with a user. That is why a request that sends a user to a route that is not
user-scoped is unaffected (the users routes above read nobody's selection), and why neither value
is redacted in the logs.

**What is scoped today:** the rule, the handshake helper and the dependency, and the session side
(#550): `session_routes` (`/api/subjects*`, `/api/sessions*`), `captures`, `ws`, `live_routes`,
`vault_status` and `session_health`, which work on the active user's handle or hand its id to
`SessionService`. Every other route still works on the handle `SessionService.open_vault()` gives,
which is the root one. `/api/health`, `/api/pair*` and `/api/users*` stay that way by design
(protocol 1.8 names them as not user-scoped); the rest change in #566 (search, the session
consumers and the per-user index) and #551 (every other content route: notes, sources, study,
generated material, ...), each declaring `active_user_vault` and working on the two handles it
returns.

### Session lifecycle -- `server/sessions.py`

`SessionService(bus, *, vault=None, sync=None, vault_settings=None, host=None, sync_interval=1.0,
end_hook_timeout=10.0, index_interval=5.0)` (on
`app.state.sessions`) owns subjects/topics listing and creation and the session state machine
`active` -> `ended`, over the vault's public functions (every call in a worker thread; lifecycle
changes serialised by one lock). On first use it opens the vault (lazily, when built without one),
pulls it (`GitSync.sync()`, in a worker thread; one `GitSync`, one `run()` loop for the whole
repository) and then scans **every user's** topics for unended sessions, so a session another PC
or another student left open is seen.

**Sessions belong to a user (#550, epic #544).** The service holds the vault's root handle and
every subject, topic and session call takes the user id first (`list_subjects(user_id)`,
`create_subject`, `list_topics`, `create_topic`, `start(user_id, ...)`, `resume(user_id,
session_id, ...)`, `require_active(user_id, session_id)`, `get_active(user_id, session_id)`,
`is_known`), working through `vault.for_user(user_id)`; `end(session_id, ...)` finds the session's
user itself. The routes pass the id of the request's active user; `None` is the single-user
fallback (the vault's only user, else `NoUserError`; an id the vault lacks is `UnknownUserError`).
`OpenSession` and the session handle carry `user_id`.

- Still one active session per backend, whichever user's it is. A `start` or `resume` by another
  user while one is unended is `OtherUserSessionOpenError` (a `SessionConflictError`), answered
  `409` code `session_open` with the Spanish detail «Otro usuario tiene una sesión de captura
  abierta en este ordenador» and **no** `X-Open-Session-Id` header; the same user keeps today's
  answer, with the header.
- A session id of user A asked for by user B is unknown (404, 4404 on the socket): never A's
  session, and its existence is never confirmed.

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
- `add_on_attached(hook)` (#425): `hook(session_id)` is called synchronously, under the lifecycle
  lock, whenever `start` or `resume` makes a session the active one (before `session.started` /
  `session.resumed` is published); a failure is logged. The capture liveness watchdog starts the
  grace period there. `end(..., reason, idle_seconds=None)`: `reason` is `EndReason`
  (`button | command | idle`); `idle` is only the watchdog's, and its `idle_seconds` goes into the
  `session.ended` payload.
- For other server code (the WebSocket gateway): `active` -> `OpenSession | None`
  (`session_id`, `user_id`, `subject_id`, `topic_id`, `started_at`, `started_at_ms`) and
  `await get_active(user_id, session_id)`, the active session only when it is that user's and
  that one.
- For the routes that write into the active session (captures): `await
  require_active(user_id, session_id)` -> the vault `Session` handle (built from the user handle), raising `UnknownSessionError`,
  `SessionAlreadyEndedError`, `SessionConflictError` (unended but not resumed) or
  `VaultUnavailableError`; `note_change()` tells `GitSync` about a vault write. The module-level
  `stored_captures(session)` -> `{capture_id: payload}` of the session's `capture.stored` events
  (blocking: call it in a worker thread).
- Refusals are `LifecycleError`s: `UnknownSessionError`, `SessionConflictError` (with
  `ActiveSessionExistsError`, `SessionAlreadyEndedError` and `VaultSyncConflictError` under it) and
  `VaultUnavailableError`; an unknown subject or topic is the vault's `SubjectNotFoundError` /
  `TopicNotFoundError`.

### Capture liveness and idle auto-end -- `server/capture_liveness.py` (#425)

A capture session stays alive only while a capture client (the web `/capture` page, the workspace
Captura tab, the Android app) is connected and sending. `CaptureLiveness(sessions, *,
grace_seconds, clock=time.monotonic, wall_clock_ms=..., interval=5.0)` (on `app.state.liveness`,
`grace_seconds` = `[server] capture_idle_end_seconds`, default 300 s) watches the session
`SessionService` last attached (`add_on_attached`, called on `start` and `resume`):

- **Sending** means at least one of the session's capture WebSockets has said `hello` (it counts
  from its `hello.ack`) and has not declared itself paused; each socket's latest `button` `pause` /
  `resume` sets its own flag, and a closed socket stops counting. The gateway reports it:
  `connected(session_id) -> token`, `set_paused(token, paused, reason=None)`,
  `disconnected(token)`; `is_sending(session_id)`, `is_held(session_id)`, `idle_seconds(session_id)`
  read it.
- **Held** (#454, protocol 1.7): a `pause` with `reason: "student"` -- the web workspace's
  Recursos tab, a page still in front of the student -- does not count as sending but keeps the
  session *held*: while that socket stays connected the idle clock does not run. `reason:
  "hidden"`, or a `pause` without reason (the Android app in the background, any 1.6 client), lets
  it run as before: the human's rule "a hidden web tab stops sending -> auto-end" stands, and a
  closed socket stops counting whatever it said. A new `pause` with the other reason replaces
  the socket's; the reason is kept in the persisted `button` event's payload.
- **Idle clock**: starts when the session is neither sending nor held, is reset as soon as a
  socket connects, resumes or is held. A session just started or resumed with no socket counts as not sending, so the grace
  starts at `session.started` / `session.resumed` (a reconnect's resume restarts it too). The
  default is longer than the web's 2-minute reconnect window (`LONG_OUTAGE_MS`, #411), so a
  dropped socket that reconnects never ends the session. The pause flag is per socket: a client
  that reconnects while paused says `pause` again right after the new `hello.ack` (the web page
  and the Android app do).
- **Auto-end**: once the idle time reaches the grace, `tick()` ends the session through the normal
  `SessionService.end(..., reason="idle", idle_seconds=...)` path (end hooks, `session.ended`,
  `end_session`, active host released, checkpoint and push). It never runs `prepare_notes` or any
  generation. `session.ended` carries `reason: "idle"` and `idle_seconds`; its origin stays `user`
  (ADR-0003's origin list is unchanged). The REST end request does not accept `idle`. The next
  `POST /api/sessions` on the topic succeeds.
- The watchdog does not need to know whose session it watches: a backend has one active session
  whichever student it belongs to, and `end` finds the session's user. The sockets it counts are
  that session's, because a handshake for another user's session is refused before it registers.
- A socket still open when the session auto-ended is closed as not active (4404) on its next
  message, like after an explicit end, and a socket of that session dialling afterwards is refused
  the same way; both close reasons end with `(idle)` (`IDLE_CLOSE_MARK`, `ended_idle(session_id)`)
  so the client can say why.
- A failed auto-end is logged, never raised: a concurrent end (`SessionConflictError`) just drops
  the watch; any other failure restarts the grace period. `start()` / `stop(timeout=15.0)` run the
  loop with the app's lifespan (stopped first at shutdown; an end in progress gets the timeout,
  then is cancelled). Only sessions `SessionService` starts or resumes are watched: review
  sessions (typed requests, #423) are opened by the vault directly and never attached.

### Session WebSocket -- `server/ws.py`

`WS /ws/sessions/{session_id}`, protocol v1 (`protocol/README.md`), served by the
`SessionGateway(bus, sessions, stt, *, sink_factory=..., provider_factory=..., clock=...,
terms_loader=...)` on
`app.state.gateway` (`ws_router()` mounts it). `sink_factory(stt, clock_offset_s)` builds each
connection's `TranscriptSink` (default `InMemoryTranscriptSink`, whose `clock_offset` is the
client-clock reading at session start in seconds); `provider_factory(stt)` builds a session's
server-side provider (default `provider_from_settings`); `clock()` is backend epoch ms;
`terms_loader(open_session)` reads the topic's terms for the vocabulary hints (default: the vault
through `sessions.open_vault()` and `server.vocabulary.load_topic_terms`, never raising). Tests
replace them.

- **Before `accept()`**: the LAN guard and the Host allowlist (1008), then
  `authenticate_websocket` (1008 without a valid token or loopback trust). The upgrade request also
  carries the active user, under the same `X-SA-User` / `sa_user` rule
  (`user_scope.resolve_websocket_user`, "Active user" above), resolved before the session is
  looked up. A handshake naming nobody on a vault with several users, or one this vault does not
  have, closes with 1008 and a reason starting `user_required:` / `user_not_found:`; a vault that
  cannot be opened closes 1011.
- **Session check** (after `accept()`, so the client can read the reason): a `session_id` that is
  not the active, attached session **of that user** (unknown, ended, unended but not resumed, or
  another user's) is closed with
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
- **Vocabulary hints** (#54, `server/vocabulary.py`): before `hello.ack` the connection reads the
  topic's terms (`load_topic_terms`: the subject's name, the topic's title, the observer's
  concepts from `load_observer_snapshot(write_back=False)` and the open pending count; a part that
  cannot be read is logged and left empty) and builds the hints with
  `stt.vocabulary_hints_from_settings` (`[stt] vocabulary_max_terms` / `vocabulary_max_chars`).
  In server mode the session's provider gets them (`set_vocabulary`) at every handshake, whatever
  the client's version; a client that negotiated 1.4+ gets them in `hello.ack.vocabulary_hints`
  (left out when empty). The connection also subscribes to `observer.state_op` (`SUBSCRIBED_KINDS`
  = `FORWARDED_KINDS` + it, never forwarded as such): an `add_concept` whose name changes the
  hints (`SessionVocabulary.apply_state_op`) hands the new list to the provider and, to a 1.4+
  client, sends a `notice` with `vocabulary_hints` and the last `pending_count` the connection
  knows (from the terms or the last forwarded notice). While that count is unknown, the new hints
  ride on the next forwarded observer `notice`; a forwarded notice carries hints only when they
  changed since the last ones sent.
- **STT status** (#222, protocol 1.5): in server mode, after every fed chunk and at each
  handshake, `SessionGateway.check_stt_status(state, t=...)` reads the provider's `status`
  (`stt.ProviderStatus`) and maps it to the client's `ok | reconnecting | unavailable` (`idle`
  and `streaming` are `ok`). When that differs from the last one (`ReceiveState.stt_status`, `ok`
  at first, so a session that never degrades publishes nothing) it logs it (WARNING when
  degraded, INFO on recovery) and publishes a persisted `stt.status` event (origin `stt`, `t` the
  session time of the chunk's end, payload `{"state", "detail"?}`). Each connection forwards it as
  `SttStatus` to a client that negotiated 1.5+, never the same state twice in a row; a client
  connecting while the provider is degraded gets the current status right after `hello.ack` (and
  the resume `ack`). A detail over 300 characters is left out, the state is still sent.
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
  (`transcript.partial`, `transcript.final`, `command`, `notice`, `capture.stored`,
  `stt.status`) before
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
- `backend/tests/server/test_pipeline_e2e.py` replays that fixture through the whole pipeline
  (gateway, client STT, capture processing, page transcriber, observer, then "prepárame el tema",
  with Claude scripted per role by `FakeClaude`) and asserts on a clone of the local remote the
  vault pushed to: transcript, pages and their transcription, events, `review/pending.yaml`, and
  notes that pass the ADR-0005 validator, tagged v1.
- `backend/tests/server/test_workspace_e2e.py` drives the workspace path end to end (#369): a
  client-mode recording built in the test (a notes page, then "incorpora esta página", "haz una
  tabla con las tres causas" and "ya está, quiero estudiar") is replayed while a client reads
  `GET .../workspace/stream`, on a virtual clock whose `sleep` lets the backend settle between
  steps (finals on the bus, `PageTranscriber.flush`, `RequestDetector.flush`, the turns run), so
  each detector call sees one new final. It asserts the stream's `request.detected`,
  `turn.started`, `turn.result` and `notes.changed` per request, the notes (the page cited and
  `incorporada` in `GET .../sources/status`, the table section), a typed
  `POST .../workspace/messages` turn, the `go_study` turn that ended the session and labelled
  the study version (`GET .../study`, `study_current: true`), "hazme un quiz de 3 preguntas" in
  the study chat (`POST .../tutor`, the quiz `listo`), a student save that leaves
  `study_current: false` and the quiz `desactualizado`, and one commit per step's write, all of
  them on the local bare remote after the shutdown. The replay's session end is not sent when
  the spoken request already ended the session, as the web capture page does on `go_study`.

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
`audio_frames_resent`, `reconnects`) plus `session_id`, `subject_id`, `topic_id`, `ended_at_ms`
and `ended_by_backend`.

A session the backend ends itself is not a failure. The spoken "ya está, quiero estudiar" runs the
`study` turn (`study_routes.switch_to_study`), which ends the session, possibly before the
recording's last step. The replay then finds the socket closed as not active, or a capture upload
or its own end refused with 409. Before raising `ReplayError` for a closed socket or a refused
upload/end it reads `GET /api/subjects/{s}/topics/{t}/sessions`; when that lists the session as
ended, it stops sending (a server-mode socket is not reconnected either) and returns with
`ended_by_backend=True` and `ended_at_ms` from the listing's `ended_at` (no end of its own). A
message sent between the end and the socket closing is counted as sent but reaches nothing. The
CLI says so in its summary.

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
  `observer`, `editor`, `sources`, `user`), `t`, `payload` (shared by every subscriber: never mutate it),
  `seq` (`None` for a notice), `schema_version`, and `persisted`.

### CLI

- `studentassistant serve [--record]`: runs the app on `server.host`:`server.port` (uvicorn with
  `proxy_headers=False`, through `serving.serve_app`; see "Shutdown" below). `--record` gives the app a `SessionRecorder` over
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
- `studentassistant feedback list [--status nuevo|triado|descartado] [--json]` (#472): the vault's
  app feedback inbox (`vault.list_feedback`), oldest first, one tab-separated line per item (`id`,
  status, kind, `YYYY-MM-DD HH:MM`, title, `#issue` when triaged; «No hay comentarios en el
  buzón.» when empty), or with `--json` the `FeedbackItem`s as a JSON list. An unreadable inbox or
  vault exits 1.
- `studentassistant feedback mark <id> --status triado|descartado|nuevo [--issue N]`: appends a
  status change (`vault.set_feedback_status`, under the vault's `feedback` lock, so it is safe
  while `serve` runs) and prints the item's new line; `--issue` records the code repository's
  issue it became (omitted: the item keeps its reference). The id is matched ignoring case and
  surrounding spaces. An unknown id, a busy lock or a refused write exits 1 with a Spanish
  message; so does an ambiguous legacy id two PCs both allocated (#476, «El identificador «fb-3»
  es ambiguo: …»), which prints every item that has it and writes nothing. Nothing is committed by the command: the next
  batch commit of the vault carries it.
- `studentassistant users list [--json]` (#549, epic #544): the students of this vault, by name,
  one tab-separated line each -- id, name, email or «sin email», «con foto»/«sin foto» -- or with
  `--json` the `UserProfile`s as a JSON list (which carries `photo` and `created_at` too, what the
  protocol's `User` leaves out). With no user yet it says so in Spanish and names `users add`. It
  opens the vault's ROOT handle, because `users/` is the repository's and no user's folder has a
  copy of it; a vault that cannot be opened, or a `profile.json` that cannot be read back, exits 1
  with a Spanish message. Reads only, so it is safe while `serve` runs.
- `studentassistant users add --name NAME [--email EMAIL]`: creates the student's folder and its
  `profile.json` (`vault.create_user`) and prints the new id on the standard output, so a script
  gets the id the selection screen will show. The name decides the id, which then never changes
  whatever the name is edited into, and two students with the same name are two folders
  (`ana-garcia` and `ana-garcia-2`), so nobody's notes are written over. Nothing is committed by
  the command: a user is ordinary vault content, so the service's next batch commit carries the
  folder, like the inbox line `feedback mark` leaves behind, and it is safe while `serve` runs. A
  name or an email the vault refuses exits 1 with its Spanish message, and so does a folder of that
  id already being there.
- `studentassistant devices` / `devices list`: the paired devices (id, name, paired-at; never a
  token). `studentassistant devices revoke <id>` removes one, and its token stops being accepted.
- `studentassistant vault migrate-users [--name NAME] [--email EMAIL] [--dry-run]` (#548, epic
  #544): a format-1 vault's root content becomes its first user's, moved with `git mv` in ONE
  commit that keeps the history, and every notes version tag is re-created under that user's
  prefix (`vault.migrate`; the whole of it in `docs/modules/vault.md`, "Migration to users", and
  the procedure for the PC's real vault in `docs/runbooks/operations.md`). Listed here because it
  is the one command that must not run while `serve` does: a running backend holds a `Vault` handle
  and a `GitSync` whose paths the move makes stale, and would commit into a repository whose
  content is on its way to another directory, so both the command's `--help` and the runbook say to
  stop the service first (`systemctl --user stop studentassistant`). It prints in Spanish what it
  moved, the new user's id, the tags re-created and the push outcome, then rebuilds the per-user
  indexes; any refusal exits 1 with the Spanish reason and changes nothing, and a vault that is
  already format 2 is told there was nothing to do. Until #549-#551 land, a backend of format 2
  refuses a format-1 vault and is not yet user-scoped: `main` in that window is not what the PC's
  service should run.

### Shutdown on SIGTERM/SIGINT -- `server/serving.py` (#466)

Uvicorn's shutdown closes the listening sockets, asks every connection to close (a capture
WebSocket gets close code 1012 and its handler returns), waits for every running request task to
finish and only then runs the lifespan shutdown (the bounded consumer stops of #408 and
`SessionService.shutdown()`, the final vault commit and push). An SSE stream never finishes on its
own, and what used to end it (`WorkspaceHub.close()`, closing the bus) runs in that lifespan
shutdown, so one open review-UI tab held the process until systemd's `TimeoutStopSec` SIGKILLed
it, skipping the final commit and push. Now:

- `serve` runs `AppServer`, a `uvicorn.Server` whose `shutdown` first sets the app's
  `ShutdownSignal` (`app.state.shutdown`, `begin_shutdown(app)`; the lifespan `finally` sets it too,
  for a `TestClient`). The open-ended streams -- `GET .../workspace/stream` and `GET /api/live` --
  are wrapped in `until_shutdown(stream, signal)`, which cancels the step the stream is parked on
  and closes its generator (releasing its subscription) the moment the signal is set, so uvicorn's
  wait for the request tasks returns at once. The bounded LLM streams (tutor, revise) are not
  wrapped; they end on their own or under the bound below.
- `[server] graceful_shutdown_seconds` (default 5) is uvicorn's `timeout_graceful_shutdown`: a
  request still running after it (a tutor turn still streaming, a slow upload) is cancelled, and
  the lifespan shutdown -- the final commit and push -- still runs after it.

Measured with `serve` on a temporary vault, an open capture WebSocket plus an open workspace
stream, and a marker event sent just before SIGTERM: before, the process was still alive 60 s
after SIGTERM; after, it exits in about 0.3 s with that event committed and pushed.
`tests/server/test_shutdown.py` runs the real `AppServer` on a loopback port with the workspace
stream, the live stream and a capture WebSocket open and asserts shutdown takes under 3 s (with a
60 s uvicorn bound, so the cancel backstop is not what passes it) and that
`SessionService.shutdown()` still ran.

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
| `max_user_photo_bytes` | `5242880` (5 MiB) | largest profile photo `PUT /api/users/{user_id}/photo` accepts (#549): the body is read as it streams in and refused with 413 the moment it goes past this. `vault.users.MAX_USER_PHOTO_BYTES` keeps a ceiling of its own at this same value, which no configuration lifts, so raising this one only turns a 413 into the vault's Spanish 422; `>= 1` |
| `allowed_hosts` | `[]` | extra names a request's `Host` may carry (the DNS-rebinding allowlist above); env as JSON, `SA_SERVER__ALLOWED_HOSTS='["mypc.local"]'` |
| `capture_idle_end_seconds` | `300` | a capture session with no capture client connected and sending for this long is ended by the backend (`reason: "idle"`, nothing generated, #425); `> 0` |
| `graceful_shutdown_seconds` | `5` | on SIGTERM the open streams end at once; a request still running after this long is cancelled before the lifespan shutdown (final vault commit and push) runs (#466); `> 0` |
| `recordings_dir` | `~/.cache/studentassistant/recordings` | where `serve --record` writes one recording directory per session id (`~` expanded); must not be inside the vault |
