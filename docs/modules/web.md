# Module: web

**Lives in:** `web/`.

## Responsibility
- **Web capture page** (`/capture`, `src/capture/`): the development capture client (ADR-0001,
  ADR-0008) -- the laptop's own camera and microphone, speaking capture protocol v1 exactly like
  the Android app. Two steps: the student picks a subject and a topic (creating either one if it
  is missing), and the page resumes that topic's `open_session_id` or starts a new session; from
  then on the capture screen runs it -- camera preview, bursts of 3 high-resolution stills
  uploaded as one multipart `POST /api/sessions/{id}/captures`, the **Capturar** / **Importante**
  / **Libro** / **Apuntes** / **Terminar** buttons, a thumbnail strip with each burst's upload
  state (subiendo / guardada / duplicada / error, a duplicate counting as stored), the transcript
  the backend normalises (partials grey, finals black, a final replacing the partials that share
  its `segment_id`) and the pending-doubts counter. It opens the session socket with `hello`
  first, keeps `hello.ack.clock_offset_ms`, and lets `hello.ack.stt_mode` choose the recognizer:
  the Web Speech API (`es-ES`) in `client` mode, microphone audio streamed as PCM16 frames in
  `server` mode. A server `capture_now` command takes a burst with that `command_id` and is
  answered with an `ack`. Denied or missing camera/microphone, a browser without
  `SpeechRecognition`, a non-secure context and a lost backend connection each get their own
  Spanish explanation. The page runs on the PC itself under loopback trust
  (`docs/modules/server.md`), so it asks for no token and stores nothing: no token, no session
  state, no offline spool (the Android app owns the spool).
- **Pairing page** (`/pair`, `src/pairing/`): asks `POST /api/pair/codes` (#89) for a one-time
  code and shows a QR of exactly `{url, code}` (`qrPayload()`), the URL and the code as text,
  and a countdown to `expires_at`; on expiry the QR gives way to a "Generar un código nuevo"
  button. Refused (403), failing and unreachable backends each get a Spanish explanation.
  Pairing happens **on the PC itself only**: the backend mints codes for loopback callers only
  and serves the web app to other LAN clients only with a bearer token (#104), so the page has
  no unauthenticated exemption. It handles only the pairing code -- never a device token -- and
  stores nothing.

Spanish review UI served by the backend (localhost trusted; other LAN clients use the pairing
token):
- Study desk: subjects/topics, per-topic card (sources, sessions, minutes, pending, materials).
- Notes viewer: rendered `apuntes.md`; clicking a provenance footnote opens the sources panel
  (zoomable page image + its transcription, transcript excerpt, book/PDF page, web snapshot);
  `[^ia]` content highlighted.
- Editor chat (streamed), applied edits shown as a diff; optional push-to-talk.
- Pending panel and the doubts-resolution flow; version history; live session view; generators.

## Skeleton (public surface)
- `web/` is a Vite + React + TypeScript (`"strict": true`) app; `npm` with a committed
  `package-lock.json`. Commands, run inside `web/`:
  - `npm run dev` -- Vite dev server. It proxies `/api` (HTTP) and `/ws` (WebSocket, `ws: true`)
    to the local backend at `http://127.0.0.1:<port>`, where `<port>` is `SA_SERVER_PORT` when
    set and `8765` otherwise (the `server.port` default of `studentassistant.config`); the rule
    is `backendTarget()` in `web/backend-target.ts`.
  - `npm run build` -- type-checks (`tsc` over `tsconfig.json` and `tsconfig.node.json`; a type
    error fails the build) and writes the bundle to `backend/src/studentassistant/server/static/`
    (`build.outDir`, emptied on each build, gitignored). The backend serves that directory at
    `/` (#85).
  - `npm test` -- vitest with Testing Library in `jsdom` (`src/test/setup.ts` loads
    `@testing-library/jest-dom`); `scripts/test.sh web` runs it with `--run`.
- `src/Router.tsx` picks the page from `window.location.pathname` (`/pair` -> `PairPage`,
  `/capture` -> `CapturePage`, `/subjects/<subject>/topics/<topic>` -> `TopicPage`, anything else
  -> `App`); the backend's SPA fallback serves the app for every non-API path, so
  no router library is used.
- `src/capture/` is the capture page. Nothing outside the directory imports it except
  `src/Router.tsx`, and inside it only `api.ts` and `sessionSocket.ts` reach the network:
  - `CapturePage.tsx`: `CapturePage` -- shows `SessionPicker` until a session is open, then
    `CaptureScreen` keyed by `session_id`, so a second session in the same visit is a new
    component and not the old one with new props. That state is all the page remembers: nothing
    of a session survives a reload.
  - `SessionPicker.tsx`: `SessionPicker({onSession?, now?})` -- subject and topic lists with
    their create forms; for the chosen topic it resumes `open_session_id` or starts a new
    session, and reports the result as `OpenedSession {session, subjectName, topicName}`.
  - `CaptureScreen.tsx`: `CaptureScreen({session, subjectName, topicName, onEnded?, now?,
    playShutter?, flashMs?})` -- the running session, where socket, transcriber and camera meet:
    it builds the transcriber `hello.ack.stt_mode` asks for, uploads each burst, renders the
    buttons, the thumbnails, the transcript and the pending counter, and owns every Spanish
    message of the page. Also exports `captureCapabilities()` (which omits `audio_format` when
    `audioStreamSupported()` is false, so the backend cannot pick a `server` mode the client
    could not obey), `shutterClick()` and `FLASH_MS`.
  - `api.ts`: the REST client -- `listSubjects()`, `createSubject(name)`, `listTopics(id)`,
    `createTopic(id, name)`, `startSession(subjectId, topicId, clientTimeMs)`,
    `resumeSession(id)`, `endSession(id, reason, clientTimeMs)` and
    `uploadCaptures(id, metadata, images)` (multipart: the `METADATA_PART` part plus one
    `image_N` part per still). Every call decodes its answer with the `src/protocol/` decoders
    and returns `ApiResult<T>` = `{kind: "ok", value} | {kind: "refused", status, detail} |
    {kind: "error", status} | {kind: "unexpected", status, expected, problem} | {kind:
    "unreachable"}`; `refused` carries the backend's own Spanish `detail` and `unexpected` is a
    2xx body that is not the message the endpoint promises, which is reported and never used.
    `failures.ts`: `describeFailure(prefix, failure)` turns one into a Spanish sentence.
  - `sessionSocket.ts`: `SessionSocket({wsPath, clientTimeMs, capabilities?, onEvent?})` --
    dials the `ws_path` of the start/resume answer (`socketUrl()`), sends `hello` first and
    exposes the handshake as `handshake: Promise<HandshakeResult>` plus the getters
    `protocolVersion`, `sttMode`, `clockOffsetMs` and `audioFormat`; sends
    `sendTranscript(segment, kind)`, `sendButton(button, clientTimeMs, source?)`,
    `sendAck(commandId, clientTimeMs)` and `sendAudio(frame)`, and `close()`. Decoded server
    events arrive through `onEvent` as `SessionSocketEvent` (`transcript`, `command`, `notice`,
    `ack`, `closed`, `failed`, `rejected`). `CAPTURE_CAPABILITIES`, `CLIENT_AUDIO_FORMAT`
    (pcm16 / 16 kHz / mono) and `WEB_SPEECH_PROVIDER` are the `hello` defaults.
  - `transcriber.ts`: the seam a provider is swapped at (ADR-0008) --
    `ClientTranscriber {readonly provider: string; start(): Promise<void>; stop(): void}`, given
    `TranscriberCallbacks {onSegment(segment, kind), onProblem?(problem)}` at construction. A
    failure is a `TranscriberProblem {code, detail, recoverable}` whose `code` is a
    `TranscriberProblemCode` (`unsupported`, `permission-denied`, `network`, `unavailable`);
    `start()` rejects with a `TranscriberError` carrying it. The two implementations:
    `webSpeechTranscriber.ts` (`WebSpeechTranscriber`, `SpeechRecognition` /
    `webkitSpeechRecognition` at `WEB_SPEECH_LANGUAGE = "es-ES"`, continuous with interim
    results, restarting itself on `end` and on recoverable errors, `webSpeechSupported()` to ask
    first) and `audioStreamTranscriber.ts` (`AudioStreamTranscriber`, `audioStreamSupported()`),
    which sends audio to an `AudioFrameSink` -- `SessionSocket.sendAudio` -- and calls
    `onSegment` never, because the transcript comes back as server `transcript.*` events.
  - `audioFrames.ts` + `pcmWorklet.ts`: the binary audio of protocol v1, and no browser API in
    the first. `encodeAudioFrame({seq, clientTimeMs, pcm})` writes `AUDIO_MAGIC` (`"SAAF"`), the
    MAJOR/MINOR version bytes, a big-endian u32 `seq` and a big-endian u64 client time in an
    `AUDIO_HEADER_SIZE` (18) byte header, followed by whole PCM16 little-endian samples; an
    out-of-range field throws `AudioFrameError`. `downmixToMono()`, `pcm16Bytes()` and
    `AudioResampler` (to `PCM_SAMPLE_RATE_HZ` = 16000) do the arithmetic. `pcmWorklet.ts` is the
    processor the audio thread runs (`PCM_WORKLET_PROCESSOR`, `PCM_WORKLET_CHUNK_MS` = 100);
    the main thread imports only its name and the `PcmWorkletChunk` shape.
  - `camera.ts`: `Camera({onLost?})` -- `start(preview?)` opens the track asking for
    `MAX_STILL_EDGE_PX` as an `ideal` edge (a wish, so a smaller camera is not refused for it),
    `takePhoto()` grabs one still (`ImageCapture.takePhoto()` where the browser has it, a canvas
    grab at the track's real `getSettings()` size where it does not), `takeBurst(trigger)`
    returns a `CapturedBurst {metadata, images}` of `BURST_LENGTH` (3) stills with a fresh
    lowercase UUID `capture_id` and one `image_N` entry per still (`imagePartName()`), and
    `stop()` releases everything. A failure is a `CameraError` with a `CameraProblemCode`
    (`unsupported`, `permission-denied`, `missing-device`, `in-use`, `lost`, `unavailable`).
  - The device and protocol modules report codes and an English `detail` for the log; the page
    owns the Spanish, one message per code.
- `src/pairing/api.ts`: `requestPairingCode()` -> `{kind: "ok", pairing} | {kind: "refused"} |
  {kind: "error", status} | {kind: "unreachable"}`, and `qrPayload(pairing)`.
- `src/topic/`: `TopicPage` (heading "Tema <topic>") is the topic page; for now it only hosts
  `PdfUploadForm` ("Añadir un PDF": a file input, an optional "Páginas" text such as `82-94`, sent
  as typed). `api.ts`: `uploadPdf(subjectId, topicId, file, pages)` posts the multipart form to
  `POST /api/subjects/{s}/topics/{t}/sources/pdf` -> `{kind: "ok", imported} | {kind: "refused",
  status, detail} | {kind: "error", status} | {kind: "unreachable"}`; a refusal's Spanish
  `detail` (413 too large, 422 unreadable or bad range) is shown as it comes.
- `src/App.tsx` is the placeholder study desk: heading "Mesa de estudio", fetches
  `GET /api/health` on mount, decodes it strictly as `rest.health.response` and shows the
  backend `protocol_version` (Spanish loading/error states; any other shape is the error state).

## Boundaries
- Talks only to the backend REST/SSE API; no direct vault or LLM access.
- The capture page adds one thing to that surface: the session WebSocket at the `ws_path` the
  start/resume answer returned. It is the only socket `web/` opens, and `src/capture/` modules
  reach the wire only through `api.ts` (REST) and `sessionSocket.ts` (WebSocket) -- the
  transcribers and the camera never call `fetch` or open a socket themselves.
- Every JSON body, in and out, is encoded and decoded by the TypeScript bindings of
  `src/protocol/`; the capture page adds no message type and no schema of its own. The one wire
  format it does implement itself is the binary audio frame, in `audioFrames.ts`, because the
  bindings carry no binary layout -- module:protocol owns that definition (`protocol/README.md`).

## Tests
vitest + Testing Library with a mocked API. `src/capture/testing/` holds the fakes the capture
tests run on, because jsdom has none of these APIs: `installCaptureFakes()` installs the media
devices / stream / track, `ImageCapture`, `SpeechRecognition`, `WebSocket` and
`AudioContext`/`AudioWorklet` fakes at once (`installMediaFakes()`,
`installSpeechRecognitionFake()`, `installWebSocketFake()` and `installAudioFakes()` install one
family each, `installCanvasFakes()` and `fakePreview()` cover the canvas fallback and the preview
element), and every installer returns a `restore()`. Tests drive them -- a fake recognition emits
results, ends and errors, a fake socket records what was sent and lets a test push server events
in -- so no test touches a real camera, microphone, network or backend.
