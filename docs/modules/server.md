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

### `create_app(static_dir=None, *, server=None, codes=None) -> FastAPI`
`studentassistant.server.app.create_app` builds a fresh app (one per caller; nothing is registered
at import time). `server` is the `[server]` config section (`ServerSettings`; default: read from
`studentassistant.config`), `codes` the in-memory `PairingCodes` (tests inject one with a fake
clock). The app keeps both, and its `DeviceStore`, in `app.state.server` / `.codes` / `.devices`.
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
