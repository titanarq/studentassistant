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

## Public surface (`studentassistant.protocol`)
Everything below is re-exported from the package root; other modules import only from there.
- Version: `PROTOCOL_VERSION` (`"1.0"`), `parse_version`, `check_compatible` (raises
  `IncompatibleProtocolVersionError`, a `ValueError` naming both versions), `negotiate` (shared
  MAJOR, lower MINOR).
- Base: `ProtocolModel`, the strict (`extra="forbid"`) and frozen Pydantic v2 base of every message.
- Client WS events: `ClientHello` (with `ClientCapabilities`, `AudioFormat`),
  `TranscriptClientPartial`, `TranscriptClientFinal`, `Button`, `Marker`, `ClientAck`; the
  discriminated union `ClientEvent`, its `CLIENT_EVENT_ADAPTER` and `parse_client_event`.
- Server WS events: `HelloAck`, `TranscriptPartial`, `TranscriptFinal`, `Command`, `Notice`,
  `ServerAck`; the union `ServerEvent`, `SERVER_EVENT_ADAPTER` and `parse_server_event`. Both parse
  functions raise `pydantic.ValidationError` on an unknown or missing `type`.
- REST bodies: `PairRequest`, `PairResponse`, `HealthResponse`, `Subject`, `SubjectsListResponse`,
  `SubjectCreateRequest`, `Topic`, `TopicsListResponse`, `TopicCreateRequest`,
  `SessionStartRequest`, `Session` (start and resume response), `SessionEndRequest`,
  `SessionEndResponse`, `CaptureUploadRequest` (with `CaptureImage`), `CaptureUploadResponse`.
- Registry: `MODELS`, mapping each `protocol/<name>.schema.json` name to its model, and
  `model_for(name)` (`KeyError` for an unregistered name).
- Audio frames: `AudioFrame`, `encode_frame`, `decode_frame`, `HEADER_SIZE`, `MAGIC`, and the
  errors `AudioFrameError`, `WrongMagicError`, `IncompatibleAudioFrameVersionError`.

## Public surface (web, `web/src/protocol/`)
Everything below is re-exported from `web/src/protocol/index.ts`; the capture page imports only
from there. Types mirror the Python models field for field; decoders are dependency-free and as
strict as the schemas (unknown fields refused, optional fields absent rather than `null`) and
throw `ProtocolDecodeError` naming the offending field.
- Version: `PROTOCOL_VERSION` (`"1.0"`), `parseVersion`, `checkCompatible` (throws
  `IncompatibleProtocolVersionError` with the same message as the backend), `negotiate`.
- Client WS events: `ClientHello` (with `ClientCapabilities`, `AudioFormat`),
  `TranscriptClientPartial`, `TranscriptClientFinal`, `Button`, `Marker`, `ClientAck`; the union
  `ClientEvent` discriminated on `type`, and `parseClientEvent`.
- Server WS events: `HelloAck`, `TranscriptPartial`, `TranscriptFinal`, `Command`, `Notice`,
  `ServerAck`; the union `ServerEvent` discriminated on `type`, and `parseServerEvent`. Both parse
  functions throw on an unknown or missing `type`.
- REST bodies: the same names as the Python list above (`PairRequest` ... `CaptureUploadResponse`),
  each with a `decode<Name>` decoder.
- Registry: `DECODERS` (schema name -> decoder), `MessageTypes`, `MessageName`, `isMessageName`,
  `parseMessage(name, data)`.

## Tests
- `backend/tests/protocol/test_examples.py` walks `protocol/*.schema.json`: each schema needs a
  same-named example and a registered model; the example validates against the schema and
  round-trips through the model without loss.
- `backend/tests/protocol/test_audio_frame.py` round-trips a frame and rejects a wrong magic and an
  incompatible MAJOR.
- `web/src/protocol/protocol.test.ts` (vitest, Node environment) reads the repository's own
  `protocol/examples/` through `web/src/test/protocolExamples.ts`: every example needs a
  registered decoder, every decoder an example, and each example round-trips to the same JSON
  value. `npm test` runs `tsc` first, so the `never` checks over `switch (event.type)` fail the
  suite when a union member and its `case` drift apart.
