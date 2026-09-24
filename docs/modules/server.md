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

### `create_app(static_dir: Path | None = None) -> FastAPI`
`studentassistant.server.app.create_app` builds a fresh app (one per caller; nothing is registered
at import time). Routes registered today:

- `GET /api/health` -> `{"status": "ok", "version": "<package version>"}`, whether or not the web
  app is built.
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
