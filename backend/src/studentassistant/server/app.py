"""The FastAPI application factory: what `studentassistant serve` runs and what the tests call.

A factory, not a module-level `app`, so uvicorn, the CLI and each test get an instance of their own
and nothing is imported -- and therefore nothing is registered or connected -- until somebody asks
for an app. Today it carries the protocol v1 health endpoint the runbooks and the packaging checks
use to see that the process is up, the two pairing endpoints, the subjects/topics/sessions REST
routes (`session_routes.py`) over the session lifecycle service and its event bus, the capture
client's session WebSocket (`ws.py`), and the built web app (`web/` -> `server/static/`) served at
`/` with an SPA fallback; every other route of the phone<->backend contract lands here later.

Every request first passes the LAN guard (loopback and private addresses only), then the Host
allowlist (against DNS rebinding), then the bearer check (`auth.py`); tokens and pairing codes are
redacted from every log record (`redaction.py`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from studentassistant import __version__
from studentassistant.config import ServerSettings, Settings, SttSettings, VaultSettings
from studentassistant.protocol.rest import HealthResponse
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.auth import BearerAuthMiddleware
from studentassistant.server.bus import SessionBus
from studentassistant.server.captures import captures_router
from studentassistant.server.cost import cost_router
from studentassistant.server.devices import DeviceStore
from studentassistant.server.network import HostAllowlistMiddleware, LanGuardMiddleware
from studentassistant.server.pairing import PairingCodes, pairing_router
from studentassistant.server.read_routes import read_router
from studentassistant.server.redaction import install_log_redaction
from studentassistant.server.session_routes import session_router
from studentassistant.server.sessions import SessionService
from studentassistant.server.ws import SessionGateway, ws_router
from studentassistant.stt import TranscriptPipeline, buffered_provider_from_settings
from studentassistant.vault import GitSync, Vault

STATIC_DIR = Path(__file__).parent / "static"
"""Where `cd web && npm run build` writes the web app: a code-layout constant, not configuration."""

NOT_BUILT_HINT = "The web app has not been built yet: run `cd web && npm run build`."

_RESERVED_PREFIXES = ("api", "ws")
"""First path segments that belong to the backend: never answered with the SPA's `index.html`."""


class NotBuiltResponse(BaseModel):
    """The answer to `GET /` while `server/static/` holds no built web app."""

    status: Literal["ok"] = "ok"
    hint: str = NOT_BUILT_HINT


def create_app(
    static_dir: Path | None = None,
    *,
    server: ServerSettings | None = None,
    codes: PairingCodes | None = None,
    vault: Vault | None = None,
    sync: GitSync | None = None,
    vault_settings: VaultSettings | None = None,
    stt: SttSettings | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app with every route this backend serves.

    `static_dir` is the built web app to serve at `/` (default `STATIC_DIR`); tests point it at a
    temporary directory. Whether it is built is decided once, here: a directory with an
    `index.html` is served, anything else gets the not-built JSON hint at `/`. `server` defaults to
    the configured `[server]` section; `codes` (the one-time pairing codes) to a fresh store.

    `vault` is the content store the session routes work on (tests pass `tmp_vault`); without one,
    the vault at `vault_settings.path` (default: the configured `[vault]` section) is opened on the
    first request that needs it, so creating an app never touches a vault. `sync` defaults to a
    `GitSync` of that vault. `stt` is the `[stt]` section the session WebSocket follows (default:
    the configured one).

    The app's lifespan drives that sync: while the app serves, an open vault gets the background
    commit/push loop (`SessionService.startup`), and shutdown stops it and flushes what is pending
    (`SessionService.shutdown`).
    """
    install_log_redaction()
    if server is None or stt is None or (vault is None and vault_settings is None):
        settings = Settings()
        server = settings.server if server is None else server
        stt = settings.stt if stt is None else stt
        vault_settings = settings.vault if vault_settings is None else vault_settings
    devices = DeviceStore(server.devices_path)
    app = FastAPI(title="Student Assistant", version=__version__, lifespan=_lifespan)
    app.state.server = server
    app.state.devices = devices
    app.state.codes = PairingCodes() if codes is None else codes
    app.state.bus = SessionBus()
    app.state.sessions = SessionService(
        app.state.bus, vault=vault, sync=sync, vault_settings=vault_settings
    )
    assert stt is not None
    app.state.gateway = SessionGateway(
        app.state.bus, app.state.sessions, stt, provider_factory=buffered_provider_from_settings
    )
    # Bus `transcript.final` events -> each session's `transcript.jsonl` (started by the lifespan).
    app.state.transcripts = TranscriptPipeline(app.state.bus, app.state.bus.attached)

    # Starlette runs the last one added first: the LAN guard, the Host allowlist (DNS rebinding),
    # then the bearer check.
    app.add_middleware(BearerAuthMiddleware, server=server, devices=devices)
    app.add_middleware(HostAllowlistMiddleware, server=server)
    app.add_middleware(LanGuardMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI echoes each invalid `input` back; a pairing code or token must never be echoed.
        errors = [
            {key: value for key, value in error.items() if key not in ("input", "ctx")}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/api/health")
    def health() -> HealthResponse:
        # Protocol v1 `rest.health.response`: strict clients reject any other key, so the package
        # version is not here (it stays in the OpenAPI metadata, `GET /openapi.json`).
        return HealthResponse(
            status="ok",
            protocol_version=PROTOCOL_VERSION,
            server_time_ms=time.time_ns() // 1_000_000,
        )

    app.include_router(pairing_router(server, devices, app.state.codes))
    app.include_router(session_router())
    app.include_router(cost_router())
    app.include_router(ws_router())
    app.include_router(captures_router())
    app.include_router(read_router())

    # The web routes go last so every API/WebSocket route registered above keeps priority.
    _add_web_routes(app, STATIC_DIR if static_dir is None else static_dir)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    sessions: SessionService = app.state.sessions
    transcripts: TranscriptPipeline = app.state.transcripts
    transcripts.start()
    await sessions.startup()
    try:
        yield
    finally:
        await transcripts.stop()
        await sessions.shutdown()


def _add_web_routes(app: FastAPI, static_dir: Path) -> None:
    root = static_dir.resolve()
    index = root / "index.html"

    if not index.is_file():

        @app.get("/")
        def not_built() -> NotBuiltResponse:
            return NotBuiltResponse()

        return

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str) -> FileResponse:
        if path.split("/", 1)[0] in _RESERVED_PREFIXES:
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        # SPA fallback: the web app's own router resolves every other path client-side.
        return FileResponse(index)
