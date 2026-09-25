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

import asyncio
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
from studentassistant.config import (
    ObserverSettings,
    ServerSettings,
    Settings,
    SourcesSettings,
    SttSettings,
    VaultSettings,
)
from studentassistant.llm import Transport
from studentassistant.observer.live import ObserverLoop, default_client_factory
from studentassistant.protocol.rest import HealthResponse
from studentassistant.protocol.version import PROTOCOL_VERSION
from studentassistant.server.auth import BearerAuthMiddleware
from studentassistant.server.bus import SessionBus
from studentassistant.server.captures import captures_router
from studentassistant.server.cost import cost_router
from studentassistant.server.devices import DeviceStore
from studentassistant.server.doubts_routes import doubts_router
from studentassistant.server.network import HostAllowlistMiddleware, LanGuardMiddleware
from studentassistant.server.notes_routes import NotesGenerator, notes_router
from studentassistant.server.pairing import PairingCodes, pairing_router
from studentassistant.server.pdf_upload import pdf_upload_router
from studentassistant.server.read_routes import read_router
from studentassistant.server.recorder import SessionRecorder
from studentassistant.server.redaction import install_log_redaction
from studentassistant.server.revise_routes import revise_router
from studentassistant.server.search_routes import search_router
from studentassistant.server.session_routes import session_router
from studentassistant.server.sessions import SessionService
from studentassistant.server.vault_status import vault_status_router
from studentassistant.server.ws import SessionGateway, ws_router
from studentassistant.sources.transcriber import PageTranscriber
from studentassistant.sources.transcriber import (
    default_client_factory as transcriber_client_factory,
)
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
    sources: SourcesSettings | None = None,
    recorder: SessionRecorder | None = None,
    llm_transport: Transport | None = None,
    llm_settings: Settings | None = None,
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
    the configured one). `sources` is the `[sources]` section the PDF upload follows (default: the
    configured one).

    The app's lifespan drives that sync: while the app serves, an open vault gets the background
    commit/push loop (`SessionService.startup`), and shutdown stops it and flushes what is pending
    (`SessionService.shutdown`).

    `recorder` (`serve --record`) records every session's client inputs through the WebSocket
    gateway and the capture upload; each recording is finished when its session ends (or the app
    shuts down). Without one nothing is recorded. A recorder whose directory is inside the vault is
    refused with `ValueError`: a recording is never vault content.

    `llm_transport` turns the live observer on (`observer/live.py`): with one, and `[observer]
    enabled` in `llm_settings` (default: the configured settings, which also give the observer
    role's model, the cost caps and the prices), every session is observed through that transport
    -- `serve` passes the real Anthropic one, tests a `FakeClaude`. Without one no Claude call is
    ever made, so an app built by a test never reaches the network. The same transport gives
    "prepárame el tema" (`POST .../notes/generate`, `notes_routes.py`) its `editor` client, and
    drives the page transcriber (`sources/transcriber.py`, `[sources] transcription_enabled`),
    which transcribes every stored capture.
    """
    install_log_redaction()
    if (
        server is None
        or stt is None
        or sources is None
        or (vault is None and vault_settings is None)
    ):
        settings = Settings()
        server = settings.server if server is None else server
        stt = settings.stt if stt is None else stt
        sources = settings.sources if sources is None else sources
        vault_settings = settings.vault if vault_settings is None else vault_settings
    if recorder is not None:
        vault_root = vault.path if vault is not None else vault_settings.path  # type: ignore[union-attr]
        if _is_within(recorder.root, vault_root):
            raise ValueError(
                f"the recordings directory {recorder.root} is inside the vault {vault_root}"
            )
    devices = DeviceStore(server.devices_path)
    app = FastAPI(title="Student Assistant", version=__version__, lifespan=_lifespan)
    app.state.server = server
    app.state.sources = sources
    app.state.devices = devices
    app.state.codes = PairingCodes() if codes is None else codes
    app.state.bus = SessionBus()
    app.state.sessions = SessionService(
        app.state.bus, vault=vault, sync=sync, vault_settings=vault_settings
    )
    assert stt is not None
    app.state.recorder = recorder
    app.state.gateway = SessionGateway(
        app.state.bus,
        app.state.sessions,
        stt,
        provider_factory=buffered_provider_from_settings,
        recorder=recorder,
    )
    # Bus `transcript.final` events -> each session's `transcript.jsonl` (started by the lifespan).
    app.state.transcripts = TranscriptPipeline(app.state.bus, app.state.bus.attached)
    # Ending a session waits for the pipeline to write every final published before the end.
    app.state.sessions.add_before_close(lambda _session_id: app.state.transcripts.drain())
    app.state.observer = None
    app.state.transcriber = None
    app.state.notes = None
    if llm_transport is not None:
        llm_settings = llm_settings or Settings()
        # "Prepárame el tema": the editor role writes the notes (`notes_routes.py`).
        app.state.notes = NotesGenerator(llm_settings, llm_transport)
        if sources.transcription_enabled:
            app.state.transcriber = PageTranscriber(
                app.state.bus,
                app.state.bus.attached,
                settings=sources,
                client_factory=transcriber_client_factory(llm_settings, llm_transport),
                on_write=app.state.sessions.note_change,
            )
            # Server start: the untranscribed pages of every topic's last sessions (#181).
            app.state.sessions.add_on_open(app.state.transcriber.catch_up_vault)
            # Before the observer's flush, so the observer sees the last pages' transcriptions.
            app.state.sessions.add_before_ended(app.state.transcriber.flush)
        observer_settings: ObserverSettings = llm_settings.observer
        if observer_settings.enabled:
            app.state.observer = ObserverLoop(
                app.state.bus,
                app.state.bus.attached,
                settings=observer_settings,
                client_factory=default_client_factory(llm_settings, llm_transport),
            )
            # Registered after the gateway's STT flush, so the observer sees the last finals.
            app.state.sessions.add_before_ended(app.state.observer.flush)
    if recorder is not None:
        app.state.sessions.add_before_close(
            lambda session_id: asyncio.to_thread(recorder.close, session_id)
        )

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
    app.include_router(pdf_upload_router())
    app.include_router(search_router())
    app.include_router(vault_status_router())
    app.include_router(notes_router())
    app.include_router(doubts_router())
    app.include_router(revise_router())

    # The web routes go last so every API/WebSocket route registered above keeps priority.
    _add_web_routes(app, STATIC_DIR if static_dir is None else static_dir)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    sessions: SessionService = app.state.sessions
    transcripts: TranscriptPipeline = app.state.transcripts
    observer: ObserverLoop | None = app.state.observer
    transcriber: PageTranscriber | None = app.state.transcriber
    transcripts.start()
    if observer is not None:
        observer.start()
    if transcriber is not None:
        transcriber.start()
    await sessions.startup()
    try:
        yield
    finally:
        await transcripts.stop()
        if transcriber is not None:
            await transcriber.stop()
        if observer is not None:
            await observer.stop()
        await sessions.shutdown()
        recorder: SessionRecorder | None = app.state.recorder
        if recorder is not None:
            recorder.close_all()


def _is_within(path: Path, root: Path) -> bool:
    return path.expanduser().resolve().is_relative_to(root.expanduser().resolve())


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
