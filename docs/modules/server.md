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

### `create_app(static_dir=None, *, server=None, codes=None, vault=None, sync=None, vault_settings=None, stt=None) -> FastAPI`
`studentassistant.server.app.create_app` builds a fresh app (one per caller; nothing is registered
at import time). `server` is the `[server]` config section (`ServerSettings`; default: read from
`studentassistant.config`), `codes` the in-memory `PairingCodes` (tests inject one with a fake
clock). `vault` is the open `Vault` the session routes work on (tests pass `tmp_vault`); without
one, the vault at `vault_settings.path` (default: the configured `[vault]` section) is opened on
the first request that needs it, so building an app never touches a vault. `sync` is the vault's
`GitSync` (default: one over that vault with `vault_settings.git`). `stt` is the `[stt]` section
(`SttSettings`, default: the configured one) the session WebSocket follows.

The app's lifespan drives that `GitSync`: on startup it calls `SessionService.startup()`, so once
the vault is open (still lazily, on the first request that needs it) `GitSync.run()` runs as a
background asyncio task and commits and pushes batched changes on its own schedule; on shutdown
`SessionService.shutdown()` cancels that task and runs `GitSync.flush()` in a worker thread, so no
noted change is left uncommitted. Without the lifespan (a `TestClient` used outside `with`) there
is no background loop.

`app.state` holds `server`, `codes`, `devices` (the `DeviceStore`), `bus` (the app-wide
`SessionBus`), `sessions` (the `SessionService` over the vault, whose `bus` is `app.state.bus`)
and `gateway` (the `SessionGateway` of the session WebSocket).
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
    `open_session_id` when it has an unended session; `POST /api/subjects/{subject_id}/topics`
    (`rest.topics.create.request`) -> 201 `rest.topics.create.response`.
  - `POST /api/sessions` (`rest.sessions.start.request`) -> 201 `rest.sessions.start.response`;
    `POST /api/sessions/{id}/resume` (no body) -> `rest.sessions.resume.response`;
    `POST /api/sessions/{id}/end` (`rest.sessions.end.request`) -> `rest.sessions.end.response`.
    `received_capture_ids` lists the captures already stored for the session (the `capture_id`s
    of its `capture.stored` events, in log order; see the capture upload below), so a resuming
    client re-uploads only the rest.
  - Errors, as `{"detail": "..."}`: an unknown subject, topic or session is 404; another session
    active or unended (start, resume) is 409 with its id in `X-Open-Session-Id`; resuming or
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
  - Stored: until capture processing exists (the `sources` module) only `images[0]` is stored,
    as it came, through `vault.put_source(vault, subject, topic, <source context>,
    "capture.<ext>", bytes, meta)` (`<ext>` from its content type: `.jpg`, `.png`, `.webp`; in a
    worker thread, followed by `GitSync.note_change()`), so under `sources/<source context>/`.
    The sidecar `meta`: `capture_id`, `session`, `captured_at` (`images[0].client_time_ms` as
    ISO 8601 UTC), `trigger`, `command_id` (when present), `image_count`, `width_px`,
    `height_px` (of `images[0]`), `source_context`. The other images are received, counted and
    validated, not stored. Then the persisted bus event `capture.stored` (origin `phone`;
    `observer.CAPTURE_EVENT_KIND`, which the observer's fold registers) is published with payload
    `capture_id`, `trigger`, `command_id` (when present), `image_count`, `client_time_ms`,
    `source_path` (the stored file, relative to the vault root) and `source_context` (the same
    value as the sidecar's). The WebSocket gateway acknowledges that event to the connected
    client (see "Forwarded to the client" below). Answer: 201 `rest.sessions.captures.response`,
    `status: "stored"`, `image_count` the images in the burst, `received_at_ms` the backend
    clock.
  - Idempotent on `capture_id`: the stored ids of a session are its `capture.stored` events
    (`sessions.stored_captures(session)`, reading `events.jsonl`, so it survives a restart). A
    stored id is answered 200 `status: "duplicate"` with the stored `image_count`, storing and
    publishing nothing. The check and the store are serialised per session, so concurrent
    uploads of one new id store it once. A session that ends between the check and the event is
    409 (the source file may stay; the observer never sees it without its event).
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
   401 for `/`. Whoever passed is in `request.state.principal` (`Principal(device_id, local)`).

**WebSocket routes** are not covered by the bearer middleware. Each one calls
`await authenticate_websocket(websocket)` (`server/auth.py`) before `accept()`. It reads the token
from `Authorization: Bearer <token>` or from the `?token=` query parameter (browsers cannot set
WebSocket headers), applies the same loopback trust, and returns the `Principal`. If
authentication fails, it closes with 1008 and returns `None`, and the route must just return.

**Paired devices** (`server/devices.py`, `DeviceStore`): `issue_token(name)`,
`verify_token(token)`, `list_devices()` and `revoke(device_id)` over the JSON file at
`server.devices_path` (default `~/.local/share/studentassistant/devices.json`, never inside the
vault). The file is written atomically with mode 600. It keeps, per device, an id, the name, the
creation time and a salted SHA-256 hash of the token (compared with `hmac.compare_digest`), never
the token itself. A token is `sa_` + `secrets.token_urlsafe(32)`. The file is re-read on every
check, so a revocation from the CLI takes effect in the running server immediately.

**Log hygiene** (`server/redaction.py`): `create_app` calls `install_log_redaction()`, which wraps
the process's log record factory. Every record, uvicorn's access and error logs included, then has
its message, string arguments and traceback redacted: `Bearer ...`, `token=...`, `sa_...`
tokens, `XXXX-XXXX` codes, and the JSON fields `token` / `pairing_code`. A 422 validation error
never echoes the request's `input` back.

### Session lifecycle -- `server/sessions.py`

`SessionService(bus, *, vault=None, sync=None, vault_settings=None, host=None, sync_interval=1.0,
end_hook_timeout=10.0)` (on
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
  The app adds `TranscriptPipeline.drain()` as a before-close hook, so every `transcript.final`
  published before `session.ended` is in `transcript.jsonl` before the session is marked ended.
- Every vault write it makes, and every persisted bus event, calls `GitSync.note_change()`.
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
- Not yet: flushing the provider (`finish()`) when a session ends, and the `capture_ids` side of
  the `ack` (the capture upload task).

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

- `studentassistant serve`: runs the app on `server.host`:`server.port` (uvicorn with
  `proxy_headers=False`).
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
