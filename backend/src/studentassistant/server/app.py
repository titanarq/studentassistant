"""The FastAPI application factory: what `studentassistant serve` runs and what the tests call.

A factory, not a module-level `app`, so uvicorn, the CLI and each test get an instance of their own
and nothing is imported -- and therefore nothing is registered or connected -- until somebody asks
for an app. Today it only carries the health endpoint the runbooks and the packaging checks use to
see that the process is up; every other route of the phone<->backend contract lands here later.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from studentassistant import __version__


class HealthResponse(BaseModel):
    """The answer to `GET /api/health`: the process is up, and this is what it is running."""

    status: Literal["ok"] = "ok"
    version: str


def create_app() -> FastAPI:
    """Build a fresh FastAPI app with every route this backend serves."""
    app = FastAPI(title="Student Assistant", version=__version__)

    @app.get("/api/health")
    def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    return app
