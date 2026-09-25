# Capture protocol v1

The wire contract between a capture client (the web capture page, the Android app) and the
backend (ADR-0001, ADR-0006, ADR-0008). This directory is the source of truth: every message type
has a JSON Schema and one example, and the Python (`studentassistant.protocol`), TypeScript and
Kotlin bindings each parse and re-serialise every example in their test suites.

Current version: **`protocol_version` 1.5**.

| version | change |
|---|---|
| 1.0 | first version |
| 1.1 | topics (`rest.topics.list.response`, `rest.topics.create.response`) gain the optional `last_session_at_ms` and `pending_count` |
| 1.2 | REST error bodies gain the optional machine-readable `code` (see "REST errors") |
| 1.3 | topics (`rest.topics.list.response`, `rest.topics.create.response`) gain the optional `digest_excerpt` |
| 1.4 | `hello.ack` and `notice` gain the optional `vocabulary_hints` (see "Vocabulary hints") |
| 1.5 | new server message `stt.status`: the server-side STT provider's state (see "STT status") |

Adding an optional field is a MINOR bump. Unknown fields stay refused, so a peer sends a field
only when the negotiated version has it: REST requests carry no version, so the backend shapes
each REST response to the `protocol_version` the device sent in `POST /api/pair` (kept in its
pairing record; a device paired before 1.1 counts as 1.0) and leaves out every field newer than
the lower MINOR. A client upgraded after pairing gets the new fields once it pairs again.

## Files and naming

| file | holds |
|---|---|
| `protocol/<name>.schema.json` | JSON Schema (draft 2020-12) of one message type |
| `protocol/examples/<name>.json` | one valid example of it |

`<name>` is `client.<type>` (client -> backend WebSocket event), `server.<type>` (backend ->
client WebSocket event) or `rest.<endpoint>.<request|response>` (REST body). The wire `type`
field keeps its own spelling (`transcript.client.final`, `hello.ack`, ...); the file name only has
to be unique and map to a model in `studentassistant.protocol.MODELS`.

Conventions shared by every message:

- Unknown fields are a contract violation and are rejected (`additionalProperties: false`).
- Times are integers in milliseconds. `client_time_ms`, `client_start_ms`, `client_end_ms` are
  Unix epoch ms on the **client's** clock; `server_time_ms` and every `*_at_ms` are Unix epoch ms
  on the **backend's** clock; `session_start_ms` / `session_end_ms` count ms since the session's
  `started_at_ms` (session time, ADR-0008).
- Ids (`subject_id`, `topic_id`, `session_id`, `device_id`, `segment_id`, `command_id`) are opaque
  strings matching `^[A-Za-z0-9][A-Za-z0-9_-]*$`. A `capture_id` is a client-generated lowercase
  hyphenated UUID.
- Student-facing text inside messages (names, transcript text, marker labels) is Spanish.

## Version negotiation

`protocol_version` is `MAJOR.MINOR`. Two peers with the same MAJOR interoperate and speak the
**lower** of the two MINORs; a different MAJOR is refused with a clear message naming both
versions, e.g.

```text
incompatible protocol_version 2.0: this side speaks 1.5; update the older side so both share MAJOR version 1
```

It is exchanged in four places:

- `POST /api/pair`: the client sends its version, the backend answers with its own; the client
  refuses to pair with an incompatible MAJOR. The backend keeps the client's version with the
  device and answers that device's REST requests in the negotiated version.
- `GET /api/health` and every session response carry the backend's version.
- The WebSocket `hello` carries the client's version; `hello.ack` carries the negotiated one. An
  incompatible MAJOR gets no `hello.ack`: the backend closes the socket with the message above.
- Every binary audio frame carries MAJOR and MINOR bytes; a frame with another MAJOR is refused.

In Python: `PROTOCOL_VERSION`, `parse_version`, `check_compatible` (raises
`IncompatibleProtocolVersionError`) and `negotiate`.

## REST

All endpoints live under `/api` and exchange JSON, except the captures upload (multipart). Every
endpoint except `GET /api/health` and `POST /api/pair` needs `Authorization: Bearer <token>`,
the token returned by pairing (never logged by either side).

| endpoint | request | response |
|---|---|---|
| `POST /api/pair` | `rest.pair.request` | `rest.pair.response` |
| `GET /api/health` | -- | `rest.health.response` |
| `GET /api/subjects` | -- | `rest.subjects.list.response` |
| `POST /api/subjects` | `rest.subjects.create.request` | `rest.subjects.create.response` |
| `GET /api/subjects/{subject_id}/topics` | -- | `rest.topics.list.response` |
| `POST /api/subjects/{subject_id}/topics` | `rest.topics.create.request` | `rest.topics.create.response` |
| `POST /api/sessions` | `rest.sessions.start.request` | `rest.sessions.start.response` |
| `POST /api/sessions/{id}/resume` | -- | `rest.sessions.resume.response` |
| `POST /api/sessions/{id}/end` | `rest.sessions.end.request` | `rest.sessions.end.response` |
| `POST /api/sessions/{id}/captures` | `rest.sessions.captures.request` (multipart `metadata` part) | `rest.sessions.captures.response` |
| `GET /api/search?q=&subject=&topic=&kinds=&limit=` | -- | `rest.search.response` |
| `POST /api/subjects/{subject_id}/topics/{topic_id}/web-pages` | `rest.topics.web_pages.create.request` | `rest.topics.web_pages.create.response` |

A new endpoint is not a version bump: its messages are new types, never new fields of an old
one, and a backend that predates it answers 404, which a client reports as "update the server".

### REST errors

A non-2xx REST answer has the body `{"detail": "<Spanish sentence for the student>"}` (a 422 from
request validation carries a list of the offending fields as `detail` instead). Since 1.2 the
refusals a client branches on also carry `code`, so no client matches the Spanish wording:

| `code` | status | meaning |
|---|---|---|
| `cost_cap_reached` | 409 | the session's or the day's cost cap is reached; the same request with `confirm_over_cap: true` goes past it |
| `doubt_closed` | 409 | the doubt was already answered, auto-resolved or dismissed |
| `session_open` | 409 | an unended session is in the way: another session when starting or resuming one (its id also in the `X-Open-Session-Id` header), or the topic's own session when resolving its doubts |

`code` is optional: other errors have none, a client must treat a missing or unknown code as "no
code" (and fall back on the status), and new codes may be added in later MINOR versions. Like
every field newer than 1.0 it is sent only to a client whose negotiated version has it: a device
paired as a 1.0 or 1.1 client gets the plain `{"detail": ...}` body. Error bodies have no schema
under `protocol/`: every client reads them leniently -- the Android app decodes no error
body at all and goes by the HTTP status only; the web reads `detail` and `code` and ignores
anything else. In Python: `ErrorCode`, `ERROR_CODE_SINCE`; in TypeScript: `ErrorCode`,
`ERROR_CODES`, `errorCode(body)`.

The web-only editor chat stream (`POST .../notes/chat`, Server-Sent Events,
`docs/modules/server.md`) reports a failure after the stream started as an `error` event
`{"status", "detail", "code"?}` that carries the same `code` (e.g. `cost_cap_reached`), sent under
the same version rule.

### Pairing and health

- `rest.pair.request`: the one-time `pairing_code` shown in the pairing QR, a `device_name`,
  `client_kind` (`web` | `android`) and the client's `protocol_version`.
- `rest.pair.response`: `device_id`, the long-lived bearer `token` and the backend's
  `protocol_version`.
- `rest.health.response`: `status: "ok"`, `protocol_version`, `server_time_ms`. Unauthenticated,
  so a client can check reachability and version before pairing.

### Subjects and topics

- `rest.subjects.list.response`: `subjects`, a list of `{subject_id, name}`.
- `rest.subjects.create.request`: `{name}`; `rest.subjects.create.response`: the created subject.
- `rest.topics.list.response`: `subject_id` and its `topics`, each `{topic_id, subject_id, name,
  open_session_id?, last_session_at_ms?, pending_count?, digest_excerpt?}`. `open_session_id` names the session
  still open on that topic: the client resumes it instead of starting a new one.
  `last_session_at_ms` (since 1.1) is the start of the topic's latest session, open or ended, on
  the backend's clock; `pending_count` (since 1.1) counts the topic's open pending-review items
  (doubts awaiting the student). Both are left out when unknown (no session yet, or state the
  backend could not read) and always for a device that paired as a 1.0 client.
  `digest_excerpt` (since 1.3) is the summary paragraph of the topic digest (`state/digest.md`,
  rewritten at every session end), at most 400 characters of Spanish text, so the student sees
  where the topic was left before continuing it; left out before the topic's first ended
  session, when the digest cannot be read, and for a device that paired as a 1.0-1.2 client.
  A just-created topic (`rest.topics.create.response`) never has one.
- `rest.topics.create.request`: `{name}`; `rest.topics.create.response`: the created topic.

### Session lifecycle

A session is about exactly one topic of one subject, fixed when it starts.

- `rest.sessions.start.request`: `subject_id`, `topic_id`, `client_time_ms`.
- `rest.sessions.start.response` and `rest.sessions.resume.response` (same shape): `session_id`,
  `subject_id`, `topic_id`, `status: "active"`, `started_at_ms`, `ws_path` (always
  `/ws/sessions/{session_id}`), `protocol_version` and `received_capture_ids`, the captures the
  backend already stored so a resuming client re-uploads only the rest.
- `rest.sessions.end.request`: `client_time_ms` and `reason` (`button` | `command`).
- `rest.sessions.end.response`: `session_id`, `status: "ended"`, `ended_at_ms`.

### Capture upload (idempotent burst)

`POST /api/sessions/{id}/captures` is `multipart/form-data`: one part named `metadata` holding the
`rest.sessions.captures.request` JSON, plus one part per image, named `image_0`, `image_1`, ...

- `rest.sessions.captures.request`: `capture_id` (client UUID), `trigger` (`button` when the
  student pressed capture, `command` when the backend sent `capture_now`), `command_id` of that
  `command` when `trigger` is `command`, `client_time_ms`, and `images` (at least one), each
  `{part, content_type (image/jpeg | image/png | image/webp), width_px, height_px,
  client_time_ms}`.
- `rest.sessions.captures.response`: `capture_id`, `session_id`, `status` (`stored` |
  `duplicate`), `image_count`, `received_at_ms`.

Re-sending a `capture_id` the backend already stored is answered with `status: "duplicate"` and
stores nothing, so a client may retry an upload or replay its offline spool freely.

### Search

`GET /api/search` searches the vault's notes, page transcriptions, PDF page text, web pages and
final transcript segments (the web's search box). Query: `q` (plain text, every word must appear
as a prefix, accents and case ignored), optional `subject` and `topic` ids (`topic` needs
`subject`), `kinds` (comma-separated subset of the hit kinds below, default all) and `limit`
(1-100, default 20).

- `rest.search.response`: the `query` and its `hits`, best first, each `{kind (notes | page | pdf
  | web | transcript), path, source?, subject, topic, session?, seq?, t_start?, snippet}`. `path`
  is the vault-relative file the text is in, `source` the vault-relative source it belongs to
  (absent for notes and transcripts); a transcript hit carries its `session`, the segment's `seq`
  and `t_start` (session time, ms). `snippet` marks each matched term between U+0002 and U+0003.

### Web pages

- `rest.topics.web_pages.create.request`: a web page the student gives by its address, stored as
  a source of the topic: `url` (http or https, at most 2000 characters) and the optional `via`,
  how it arrived (`url`: pasted in the web UI, the default; `share`: shared to the phone app).
- `rest.topics.web_pages.create.response`: the stored snapshot, `{source_id, vault_id, title,
  url, already_kept}`: `source_id` is `sources/web/NNN-<slug>.md` (what provenance cites),
  `vault_id` its vault-relative path, `url` the address it was fetched from. 201 when the page
  was fetched and stored; 200 with `already_kept: true` when the topic already had that address
  (nothing fetched). Refusals: 404 unknown topic, 409 cost cap (`cost_cap_reached`), 422 not an
  http(s) address or not a text page, 502 Claude could not fetch it, 503 no web tools.

## WebSocket `/ws/sessions/{id}`

Opened on the `ws_path` a session start/resume returned (how the socket carries the bearer
token is the server module's to define). Text messages are JSON objects discriminated by `type`; binary messages are audio frames
(server STT mode only). A message with an unknown or missing `type` is rejected, never ignored.

### Flow

1. The client sends `hello` as its first message.
2. The backend checks the version (see above), picks the STT mode, computes the clock offset and
   answers `hello.ack`.
3. In `client` STT mode the client sends `transcript.client.partial` / `transcript.client.final`
   from its own recognizer; in `server` mode it streams binary audio frames instead and the
   backend runs the STT provider.
4. Either way the backend sends back the normalised `transcript.partial` / `transcript.final`
   (session time), so every client shows the same live transcript.
5. Throughout the session the client sends `button` and `marker`; the backend sends `command`
   (answered by the client `ack`), `notice` and its own `ack` of audio and captures.

### Clock sync

`hello.client_time_ms` is the client clock when `hello` was sent. `hello.ack.clock_offset_ms` is
the backend clock minus that reading (may be negative): adding it to any client time gives backend
time. `hello.ack.server_time_ms` is the backend clock when it answered.

### Client events

| name | `type` | fields |
|---|---|---|
| `client.hello` | `hello` | `protocol_version`, `capabilities`, `client_time_ms` |
| `client.transcript.client.partial` | `transcript.client.partial` | segment fields below |
| `client.transcript.client.final` | `transcript.client.final` | segment fields below |
| `client.button` | `button` | `button`, `source?`, `client_time_ms` |
| `client.marker` | `marker` | `client_time_ms`, `label?` (1-200 chars) |
| `client.ack` | `ack` | `command_id`, `client_time_ms` |

- `hello.capabilities`: `stt` (`client` | `server`, the client's preferred STT mode; the backend
  may still ask for the other one), `stt_provider` (id of the client's recognizer, e.g.
  `web-speech`, `android-speech`) and `audio_format` (`{encoding: "pcm16", sample_rate_hz: 16000,
  channels: 1}`), present only when the client can stream audio.
- Transcript segment fields: `segment_id` (client-assigned; the partials and the final of one
  utterance share it), `client_start_ms`, `client_end_ms` (not before start), `text`, `provider`,
  `language` (BCP 47, e.g. `es-ES`) and optional `confidence` in [0, 1]. A partial is interim text
  a later message replaces; the final replaces every partial with the same `segment_id`.
- `button`: the button equivalent of an ADR-0006 voice command: `next_page`, `important`,
  `switch_source`, `pause`, `resume`, `end_session`, `web_search`. `source` (`book` | `notes` |
  `pdf`) is required with `switch_source` and forbidden otherwise. Capture is not a button event:
  the client takes the stills itself and uploads them with `trigger: button`.
- `marker`: a point on the session timeline the student flagged.
- `ack`: the client received the `command` with that `command_id` and acted on it.

### Server events

| name | `type` | fields |
|---|---|---|
| `server.hello.ack` | `hello.ack` | `protocol_version`, `stt_mode`, `audio_format?`, `clock_offset_ms`, `server_time_ms`, `vocabulary_hints?` |
| `server.transcript.partial` | `transcript.partial` | normalised segment fields below |
| `server.transcript.final` | `transcript.final` | normalised segment fields below |
| `server.command` | `command` | `command_id`, `command`, `server_time_ms` |
| `server.notice` | `notice` | `pending_count`, `server_time_ms`, `vocabulary_hints?` |
| `server.stt.status` | `stt.status` | `state`, `detail?`, `server_time_ms` (since 1.5) |
| `server.ack` | `ack` | `audio_seq?`, `capture_ids?`, `server_time_ms` |

- `hello.ack`: `protocol_version` is the negotiated one; `stt_mode` is `client` or `server`;
  `audio_format` is present exactly when `stt_mode` is `server` and names the audio to stream.
- Normalised segment fields: `segment_id`, `session_start_ms`, `session_end_ms` (session time,
  end not before start), `text` as the backend normalised it, `language`, optional `confidence`.
- `command`: a deterministic voice command asks the client to act (ADR-0006). v1 defines
  `capture_now`: take a burst of stills and upload them with `trigger: command` and this
  `command_id`. The client answers with `ack`.
- `notice`: status the client shows the student; `pending_count` is how many doubts await review.
  Since 1.4 a notice may also carry new `vocabulary_hints` (below); it then repeats the current
  `pending_count`.
- `ack`: `audio_seq` is the highest audio frame `seq` received so far (server STT mode only);
  `capture_ids` lists stored captures (at least one when present). At least one of the two is
  present.

### Vocabulary hints

Since 1.4. `vocabulary_hints` is a list of 1 to 50 Spanish terms (each 1 to 100 characters), most
important first, that a client-side recognizer may be biased towards when it accepts phrase hints
(a grammar, a contextual-biasing list): the subject's name, the topic's name, then the concepts the
observer has extracted for the topic, newest first. The backend caps the list further with
`[stt] vocabulary_max_terms` / `vocabulary_max_chars` and leaves it out when it is empty.

- `hello.ack.vocabulary_hints`: the session's hints when the socket opens (subject, topic and the
  concepts folded so far, also those of earlier sessions of the topic).
- `notice.vocabulary_hints`: the whole new list whenever it changes during the session (the
  observer extracted a new concept); it replaces the previous one.

A client that cannot use hints ignores them. They are sent only to a client whose negotiated
version is 1.4 or higher; the backend applies the same hints to its own server-side provider (e.g.
Whisper `hotwords`) whatever the client's version.

### STT status

Since 1.5, server STT mode only. `stt.status` tells the client whether the backend's own
speech-to-text provider is transcribing the audio it streams, so the student learns that what they
say is not being written down (e.g. a cloud provider lost its connection) instead of finding a gap
in the transcript later.

- `state`: `ok` (transcribing; the client clears any STT warning it shows), `reconnecting` (the
  provider lost its service and retries; the audio meanwhile is not transcribed) or `unavailable`
  (it cannot work this session at all, e.g. missing installation or credentials; no audio is
  transcribed).
- `detail`: a Spanish sentence for the student saying what happens, 1 to 300 characters, sent
  with `reconnecting` and `unavailable` and left out with `ok`.

The backend sends it once per change of `state` (a session starts `ok`, which is never announced),
and right after `hello.ack` to a client that connects while the provider is degraded. The client
keeps streaming audio whatever the state (a provider that reconnects needs it). It is sent only to
a client whose negotiated version is 1.5 or higher; an older one gets nothing.

## Binary audio frames

Sent by the client only after `hello.ack` chose `stt_mode: "server"`. Each binary WebSocket
message is one frame: an 18-byte header, big-endian, followed by the payload.

| offset | size | field |
|---|---|---|
| 0 | 4 | magic, ASCII `SAAF` |
| 4 | 1 | `protocol_version` MAJOR (u8) |
| 5 | 1 | `protocol_version` MINOR (u8) |
| 6 | 4 | `seq`, u32: per-session audio frame counter, acknowledged by the server `ack` |
| 10 | 8 | client time in ms, u64: client-clock capture time of the first sample |
| 18 | ... | payload: PCM16, 16 kHz, mono, little-endian signed 16-bit samples |

The payload is a whole number of samples. The backend refuses a frame with a wrong magic or
another MAJOR version; a different MINOR is accepted. In Python: `AudioFrame`, `encode_frame`,
`decode_frame`, `HEADER_SIZE`, `MAGIC` and the errors `AudioFrameError`, `WrongMagicError`,
`IncompatibleAudioFrameVersionError`.

## Changing the protocol

Proposed rule of thumb, following the negotiation above:

- Adding an optional field or a new message type is a MINOR bump; removing or renaming a field,
  changing its meaning or making it required is a MAJOR bump.
- A change lands the schema, the example, the Python model and its `MODELS` entry together;
  `backend/tests/protocol/test_examples.py` fails for a schema without an example or a model.
