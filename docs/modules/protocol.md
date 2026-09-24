# Module: protocol

**Lives in:** `protocol/` (JSON Schemas `*.schema.json` + `examples/*.json`),
`backend/src/studentassistant/protocol/` (Pydantic models), `android/app/src/main/.../protocol/`
(Kotlin `@Serializable` classes), `web/src/protocol/` (TypeScript types).

## Responsibility
The capture-client (web page, Android)<->backend contract (ADR-0001, ADR-0008), documented in `protocol/README.md`:
- REST: `POST /api/pair`, `GET /api/health`, subjects/topics listing and creation, session
  start/resume/end, `POST /api/sessions/{id}/captures` (multipart burst, idempotent `capture_id`).
- WebSocket `/ws/sessions/{id}`: JSON client events (`hello` with capabilities and clock sync,
  `transcript.client.partial/final`, `button`, `marker`, `ack`), optional binary audio frames in
  server STT mode (header: `seq`, client time in ms, then PCM16 16 kHz mono), JSON server events (`transcript.partial`, `transcript.final`, `command` e.g.
  `capture_now`, `notice` e.g. pending count, `ack` of audio seq / captures).
- A `protocol_version`; both sides refuse an incompatible major version with a clear message.

## Boundaries
- Pure data definitions and (de)serialisation; no I/O.
- Every message type has an example in `protocol/examples/` that the Python, TypeScript and Kotlin
  test suites parse and re-serialise (the shared fixtures are the contract test).
