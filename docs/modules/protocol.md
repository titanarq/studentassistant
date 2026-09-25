# Module: protocol

**Lives in:** `protocol/` (JSON Schemas `*.schema.json` + `examples/*.json`),
`backend/src/studentassistant/protocol/` (Pydantic models), `android/app/src/main/.../protocol/`
(Kotlin `@Serializable` classes), `web/src/protocol/` (TypeScript types).

## Responsibility
The capture-client (web page, Android)<->backend contract (ADR-0001, ADR-0008), documented in `protocol/README.md`:
- REST: `POST /api/pair`, `GET /api/health`, subjects/topics listing and creation, session
  start/resume/end, `POST /api/sessions/{id}/captures` (multipart burst, idempotent `capture_id`),
  `GET /api/search` (the web's search over the vault index).
- WebSocket `/ws/sessions/{id}`: JSON client events (`hello` with capabilities and clock sync,
  `transcript.client.partial/final`, `button`, `marker`, `ack`), optional binary audio frames in
  server STT mode (header: `seq`, client time in ms, then PCM16 16 kHz mono), JSON server events (`transcript.partial`, `transcript.final`, `command` e.g.
  `capture_now`, `notice` e.g. pending count, `ack` of audio seq / captures).
- REST error bodies `{"detail", "code"?}` (`code` since 1.2: `cost_cap_reached`,
  `doubt_closed`, `session_open`; `protocol/README.md` "REST errors"). Not a schema'd message:
  clients read error bodies leniently.
- A `protocol_version`; both sides refuse an incompatible major version with a clear message.
  Peers speak the lower MINOR: REST requests carry no version, so the backend answers a device's
  REST calls in the version it sent at pairing (stored with the device, 1.0 when absent) and
  omits fields newer than that; the WebSocket negotiates in `hello` / `hello.ack`.

## Boundaries
- Pure data definitions and (de)serialisation; no I/O.
- Every message type has an example in `protocol/examples/` that the Python, TypeScript and Kotlin
  test suites parse and re-serialise (the shared fixtures are the contract test).

## Public surface (`studentassistant.protocol`)
Everything below is re-exported from the package root; other modules import only from there.
- Version: `PROTOCOL_VERSION` (`"1.3"`), `parse_version`, `check_compatible` (raises
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
  `SessionEndResponse`, `CaptureUploadRequest` (with `CaptureImage`), `CaptureUploadResponse`,
  `SearchResponse` (with `SearchHit`). `Topic` carries the optional `last_session_at_ms`,
  `pending_count` (1.1) and `digest_excerpt` (1.3, at most `DIGEST_EXCERPT_MAX` = 400 chars).
- REST error codes: `ErrorCode` (a `StrEnum`: `COST_CAP_REACHED`, `DOUBT_CLOSED`,
  `SESSION_OPEN`) and `ERROR_CODE_SINCE` (`(1, 2)`); the server's `server.errors` puts them in
  error bodies.
- Registry: `MODELS`, mapping each `protocol/<name>.schema.json` name to its model, and
  `model_for(name)` (`KeyError` for an unregistered name).
- Audio frames: `AudioFrame`, `encode_frame`, `decode_frame`, `HEADER_SIZE`, `MAGIC`, and the
  errors `AudioFrameError`, `WrongMagicError`, `IncompatibleAudioFrameVersionError`.

## Public surface (web, `web/src/protocol/`)
Everything below is re-exported from `web/src/protocol/index.ts`; the capture page imports only
from there. Types mirror the Python models field for field; decoders are dependency-free and as
strict as the schemas (unknown fields refused, optional fields absent rather than `null`) and
throw `ProtocolDecodeError` naming the offending field.
- Version: `PROTOCOL_VERSION` (`"1.3"`), `parseVersion`, `checkCompatible` (throws
  `IncompatibleProtocolVersionError` with the same message as the backend), `negotiate`.
- Client WS events: `ClientHello` (with `ClientCapabilities`, `AudioFormat`),
  `TranscriptClientPartial`, `TranscriptClientFinal`, `Button`, `Marker`, `ClientAck`; the union
  `ClientEvent` discriminated on `type`, and `parseClientEvent`.
- Server WS events: `HelloAck`, `TranscriptPartial`, `TranscriptFinal`, `Command`, `Notice`,
  `ServerAck`; the union `ServerEvent` discriminated on `type`, and `parseServerEvent`. Both parse
  functions throw on an unknown or missing `type`.
- REST bodies: the same names as the Python list above (`PairRequest` ... `CaptureUploadResponse`),
  each with a `decode<Name>` decoder.
- REST error codes: `ErrorCode`, `ERROR_CODES`, `isErrorCode` and `errorCode(body)` (the known
  `code` of an error body, `null` when missing or unknown).
- Registry: `DECODERS` (schema name -> decoder), `MessageTypes`, `MessageName`, `isMessageName`,
  `parseMessage(name, data)`.

## Android (Kotlin)
Package `com.titanarq.studentassistant.protocol` in `android/app/src/main/java/`, on
kotlinx.serialization (plugin + `kotlinx-serialization-json`, both from
`android/gradle/libs.versions.toml`):
- Version: `PROTOCOL_VERSION` (`"1.3"`, for the topic's `digest_excerpt`; the 1.2 error `code`
  needs nothing from the app, which decodes no error body, only the HTTP status),
  `parseVersion` (-> `ProtocolVersion`), `isCompatible`,
  `checkCompatible` (throws `IncompatibleProtocolVersionException`, an `IllegalArgumentException`
  with the same message as the backend's) and `negotiate`.
- Client WS events: the sealed `ClientEvent` (`Hello` with `ClientCapabilities` / `AudioFormat`,
  `TranscriptClientPartial`, `TranscriptClientFinal`, `Button`, `Marker`, `ClientAck`); server WS
  events: the sealed `ServerEvent` (`HelloAck`, `TranscriptPartial`, `TranscriptFinal`, `Command`,
  `Notice`, `ServerAck`). Both are discriminated on the wire field `type` (`@SerialName` +
  `@JsonClassDiscriminator`); `decodeClientEvent` / `decodeServerEvent` throw
  `SerializationException` on an unknown or missing `type`, and `encodeClientEvent` /
  `encodeServerEvent` write it.
- REST bodies: the same names as the Python models (`PairRequest`, `PairResponse`, ...,
  `CaptureUploadRequest`, `CaptureUploadResponse`); literal-valued fields are Kotlin enums.
- `ProtocolJson`, the codec every class goes through: unknown fields rejected, absent optionals
  decoded as `null` and omitted on encode.
- Registry: `MESSAGE_CODECS`, mapping each `protocol/<name>.schema.json` name to a `MessageCodec`
  (declared class + decode/encode), and `codecFor(name)`.
- Binary audio frames: `AudioFrame(seq, clientTimeMs, samples: ShortArray)` with `encode()` and
  `AudioFrame.decode(bytes)` (wrong magic, short or odd-sized payload -> `IllegalArgumentException`;
  another MAJOR -> `IncompatibleProtocolVersionException`), `HEADER_SIZE`, `MAGIC`, `MAX_SEQ`
  (u32). Tested in `AudioFrameTest.kt` against hand-written header bytes.

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
- `android/app/src/test/.../protocol/ProtocolExamplesTest.kt` decodes every shared example through
  `MESSAGE_CODECS`, checks the declared class and that re-encoding gives the same JSON value; an
  example with no codec (or a codec with no example) fails it. The examples are not copied: the
  `:app` test source set adds the repository's `protocol/` as a resources directory
  (`sourceSets.test.resources.srcDir(rootProject.file("../protocol"))`), so they are read from the
  classpath as `examples/<name>.json`.
