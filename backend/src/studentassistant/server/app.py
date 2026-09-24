"""The FastAPI application factory: what `studentassistant serve` runs and what the tests call.

A factory, not a module-level `app`, so uvicorn, the CLI and each test get an instance of their own
and nothing is imported -- and therefore nothing is registered or connected -- until somebody asks
for an app. Today it carries the health endpoint the runbooks and the packaging checks use to see
that the process is up, and the built web app (`web/` -> `server/static/`) served at `/` with an
SPA fallback; every other route of the phone<->backend contract lands here later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from studentassistant import __version__

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


def create_app(static_dir: Path | None = None) -> FastAPI:
    """Build a fresh FastAPI app with every route this backend serves.

    `static_dir` is the built web app to serve at `/` (default `STATIC_DIR`); tests point it at a
    temporary directory. Whether it is built is decided once, here: a directory with an
    `index.html` is served, anything else gets the not-built JSON hint at `/`.
    """
    app = FastAPI(title="Student Assistant", version=__version__)

    @app.get("/api/health")
    def health() -> HealthResponse:
        return HealthResponse(version=__version__)

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
