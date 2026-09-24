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

### `create_app(static_dir=None, *, server=None, codes=None, vault=None, sync=None, vault_settings=None) -> FastAPI`
`studentassistant.server.app.create_app` builds a fresh app (one per caller; nothing is registered
at import time). `server` is the `[server]` config section (`ServerSettings`; default: read from
`studentassistant.config`), `codes` the in-memory `PairingCodes` (tests inject one with a fake
clock). `vault` is the open `Vault` the session routes work on (tests pass `tmp_vault`); without
one, the vault at `vault_settings.path` (default: the configured `[vault]` section) is opened on
the first request that needs it, so building an app never touches a vault. `sync` is the vault's
`GitSync` (default: one over that vault with `vault_settings.git`).

The app's lifespan drives that `GitSync`: on startup it calls `SessionService.startup()`, so once
the vault is open (still lazily, on the first request that needs it) `GitSync.run()` runs as a
background asyncio task and commits and pushes batched changes on its own schedule; on shutdown
`SessionService.shutdown()` cancels that task and runs `GitSync.flush()` in a worker thread, so no
noted change is left uncommitted. Without the lifespan (a `TestClient` used outside `with`) there
is no background loop.

`app.state` holds `server`, `codes`, `devices` (the `DeviceStore`), `bus` (the app-wide
`SessionBus`) and `sessions` (the `SessionService` over the vault, whose `bus` is `app.state.bus`).
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
    `received_capture_ids` is always `[]` until the capture upload endpoint exists.
  - Errors, as `{"detail": "..."}`: an unknown subject, topic or session is 404; another session
    active or unended (start, resume) is 409 with its id in `X-Open-Session-Id`; resuming or
    ending an ended session is 409; a start refused because pulling the vault hit a conflict is
    409 with the conflicting paths in `detail`; a vault that cannot be opened is 503.
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

`SessionService(bus, *, vault=None, sync=None, vault_settings=None, host=None, sync_interval=1.0)` (on
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
- Every vault write it makes, and every persisted bus event, calls `GitSync.note_change()`.
- For other server code (the WebSocket gateway): `active` -> `OpenSession | None`
  (`session_id`, `subject_id`, `topic_id`, `started_at`, `started_at_ms`) and
  `get_active(session_id)`, the active session only when it is that one.
- Refusals are `LifecycleError`s: `UnknownSessionError`, `SessionConflictError` (with
  `ActiveSessionExistsError`, `SessionAlreadyEndedError` and `VaultSyncConflictError` under it) and
  `VaultUnavailableError`; an unknown subject or topic is the vault's `SubjectNotFoundError` /
  `TopicNotFoundError`.

### Session event bus -- `server/bus.py`

`SessionBus(on_append=None, default_queue_size=256)` (on `app.state.bus`) is the in-process
publish/subscribe every session event goes through; stt, sources, observer, editor and the
WebSocket gateway publish and subscribe here.

- `attach(session)` / `detach(session_id)` / `is_attached(session_id)`: the lifecycle service
  attaches the vault `Session` handle of the active session; publishing for any other session is
  refused (`SessionNotAttachedError`, a `BusError`).
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
| `allowed_hosts` | `[]` | extra names a request's `Host` may carry (the DNS-rebinding allowlist above); env as JSON, `SA_SERVER__ALLOWED_HOSTS='["mypc.local"]'` |
