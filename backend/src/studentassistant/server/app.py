"""The FastAPI application factory: what `studentassistant serve` runs and what the tests call.

A factory, not a module-level `app`, so uvicorn, the CLI and each test get an instance of their own
and nothing is imported -- and therefore nothing is registered or connected -- until somebody asks
for an app. Today it carries the health endpoint the runbooks and the packaging checks use to see
that the process is up, the two pairing endpoints, and the built web app
(`web/` -> `server/static/`) served at `/` with an SPA fallback; every other route of the
phone<->backend contract lands here later.

Every request first passes the LAN guard (loopback and private addresses only), then the bearer
check (`auth.py`); tokens and pairing codes are redacted from every log record (`redaction.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from studentassistant import __version__
from studentassistant.config import ServerSettings, Settings
from studentassistant.server.auth import BearerAuthMiddleware
from studentassistant.server.devices import DeviceStore
from studentassistant.server.network import LanGuardMiddleware
from studentassistant.server.pairing import PairingCodes, pairing_router
from studentassistant.server.redaction import install_log_redaction

STATIC_DIR = Path(__file__).parent / "static"
"""Where `cd web && npm run build` writes the web app: a code-layout constant, not configuration."""

NOT_BUILT_HINT = "The web app has not been built yet: run `cd web && npm run build`."

_RESERVED_PREFIXES = ("api", "ws")
"""First path segments that belong to the backend: never answered with the SPA's `index.html`."""


class HealthResponse(BaseModel):
    """The answer to `GET /api/health`: the process is up, and this is what it is running."""

    status: Literal["ok"] = "ok"
    version: str


class NotBuiltResponse(BaseModel):
    """The answer to `GET /` while `server/static/` holds no built web app."""

    status: Literal["ok"] = "ok"
    hint: str = NOT_BUILT_HINT


def create_app(
    static_dir: Path | None = None,
    *,
    server: ServerSettings | None = None,
    codes: PairingCodes | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app with every route this backend serves.

    `static_dir` is the built web app to serve at `/` (default `STATIC_DIR`); tests point it at a
    temporary directory. Whether it is built is decided once, here: a directory with an
    `index.html` is served, anything else gets the not-built JSON hint at `/`. `server` defaults to
    the configured `[server]` section; `codes` (the one-time pairing codes) to a fresh store.
    """
    install_log_redaction()
    server = Settings().server if server is None else server
    devices = DeviceStore(server.devices_path)
    app = FastAPI(title="Student Assistant", version=__version__)
    app.state.server = server
    app.state.devices = devices
    app.state.codes = PairingCodes() if codes is None else codes

    # Starlette runs the last one added first: the LAN guard, then the bearer check.
    app.add_middleware(BearerAuthMiddleware, server=server, devices=devices)
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
        return HealthResponse(version=__version__)

    app.include_router(pairing_router(server, devices, app.state.codes))

    # The web routes go last so every API/WebSocket route registered above keeps priority.
    _add_web_routes(app, STATIC_DIR if static_dir is None else static_dir)
    return app


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
