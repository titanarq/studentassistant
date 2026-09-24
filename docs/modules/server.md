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
