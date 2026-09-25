# Module: android

**Lives in:** `android/` (`:app`).

## Responsibility
Thin capture client (ADR-0001), Spanish UI:
- Pairing: scan the backend's QR (URL + one-time code), exchange for a token, store it in
  DataStore; several backends allowed; connection test.
- Home: subjects/topics from the backend, create topic, start or continue a session.
- Capture screen: CameraX preview, transcription with SpeechRecognizer (Google) sent as
  segments, or AudioRecord PCM16 streaming in server STT mode (ADR-0008), buttons (Capturar, Importante, Libro/Apuntes, Terminar), live transcript,
  pending-doubts counter, screen kept on.
- Still capture: burst of 3 full-resolution photos on button or `capture_now`; haptic + shutter
  sound; upload with retries; thumbnail strip (see "Still capture (#46)").
- Offline resilience: disk spool of audio, transcript lines, session events and photos while
  disconnected, resent in order on reconnect; an end while offline is completed later (see
  "Offline spool (#53)").

## Boundaries
- No study logic, no LLM calls, no vault access. Talks only the `protocol` contract.

## Backend client and pairing (#33)

- **`backend.BackendClient`** (interface) covers every REST endpoint of protocol v1: `pair`,
  `health`, `listSubjects`/`createSubject`, `listTopics`/`createTopic`,
  `startSession`/`resumeSession`/`endSession` and `uploadCapture` (multipart: a `metadata` JSON
  part plus `image_N` parts, typed by each `CaptureImage.content_type`). Authenticated calls take
  `BackendCredentials(baseUrl, token)` and send `Authorization: Bearer <token>`; `pair` and
  `health` take a bare base URL. Every call returns a `BackendResult`: `Success(value)` or a
  `Failure` -- `HttpError(status)`, `Unreachable(reason)`, `IncompatibleVersion(peer, ours)`
  (pair, health and session start/resume answers are checked with `protocol.isCompatible`) or
  `InvalidResponse(reason)` (a 2xx body that breaks the contract; decoding is `ProtocolJson`,
  strict). Nothing is thrown to the UI.
- **`backend.OkHttpBackendClient`** implements it with OkHttp (async, cancelled with the
  coroutine; no logging interceptor). **`backend.FakeBackendClient`** is the scripted fake for
  view-model tests: one `var ...Result` per endpoint, the calls recorded in `calls`.
- **`backend.BackendStore`**: the paired backends (`PairedBackend(baseUrl, deviceId, token,
  displayName)`) in a Jetpack DataStore JSON file, `filesDir/paired_backends.json`.
  `backends: Flow<PairedBackends>`, `current()`, `active()`, `save()` (adds or replaces the entry
  with the same base URL or device id and makes it active), `setActive(deviceId)`,
  `remove(deviceId)` (the first remaining becomes active). A corrupt file resets to empty. Tokens
  are protected only by app-private storage (no extra encryption); `android:allowBackup="false"`
  and `data_extraction_rules.xml` keep them off cloud backup and device transfer, and
  `toString()` of every type holding one redacts it.
- **Pairing flow** (`pairing` package): `QrScanner` asks for `CAMERA` at runtime (Spanish
  rationale), shows a CameraX preview and feeds each frame's Y plane to `QrDecoder` (ZXing core,
  no Play Services). `PairingPayload.parseQr` reads the backend's `{"url", "code"}` JSON;
  `PairingPayload.fromManualEntry` builds the same payload from the manual form (a URL without a
  scheme is taken as `http://`). `PairingViewModel` sends `POST /api/pair` with
  `client_kind: "android"`, `Build.MODEL` as the device name and `PROTOCOL_VERSION`, refuses an
  incompatible MAJOR, stores the backend as active and exposes `PairingUiState`
  (`Idle`/`Pairing`/`Paired`/`Failed(PairingFailure)`). A QR is only taken while `Idle`, so a
  rejected code is not retried on every frame.
- **Screens** (Spanish, `res/values/strings.xml`; failure messages in `ui/Messages.kt`): pairing
  (QR + manual form), paired backends (`PairedBackendsViewModel`: switch active, remove with
  confirmation) and connection test (`ConnectionTestViewModel`: `GET /api/health`, then
  `GET /api/subjects` with the token, each outcome shown). `AppContainer(filesDir, deviceName)`
  holds `backendClient`, `backendStore` and the three view-model factories; `MainActivity`
  switches between the screens (`ui.Route`) and opens pairing when no backend is stored.
- **Cleartext**: the backend serves plain HTTP on the LAN, and its address is only known at
  pairing time, so `res/xml/network_security_config.xml` permits cleartext for every host (system
  CAs only). The manifest declares `CAMERA` and `INTERNET`.
- Known gap: the backend's `GET /api/health` still answers `{status, version}`, not v1's
  `rest.health.response`, so the health check reports `InvalidResponse` until the server conforms.

## Home: subjects, topics and sessions (#37)

- **`home.HomeViewModel(client, store, sessions, clock)`** drives the home screen on the active
  backend, exposing `HomeUiState`: `subjects` and the selected subject's `topics` as
  `Loadable` (`Loading`/`Loaded`/`Failed(BackendResult.Failure)`), the create-topic dialog
  (`CreateTopicDialog(saving, failure)`, null when closed), `session: SessionAction`
  (`Idle`/`Opening(topicId)`/`Failed(topicId, SessionFailure)`) and `openedSession`, a one-shot
  the screen consumes with `onSessionShown()` to navigate. `load()` runs every time the home is
  shown (a switched backend resets the state; the selected subject is kept while it exists).
- Topic rows are `TopicRow(topic, lastSessionAtMs?, pendingCount?)`; `canContinue` is
  `topic.open_session_id != null`. `lastSessionAtMs` / `pendingCount` are the topic's
  `last_session_at_ms` / `pending_count` (protocol 1.1), and the date and doubts count are shown
  only when present (a 1.0 backend sends neither).
- **Create topic** (`createTopic(subjectName, title)`): the dialog takes a subject name (typed, or
  one tap on an existing one) and a title. A name matching an existing subject (trimmed, ignoring
  case) reuses it; otherwise `POST /api/subjects` runs first. Then `POST .../topics`, the dialog
  closes and that subject's topics are shown. Failures stay in the dialog.
- **"Empezar sesión" / "Continuar"** (`startOrContinue(row)`): `POST /api/sessions` with the
  clock's `client_time_ms`, or `POST /api/sessions/{open_session_id}/resume`. On success the
  `session.OpenSession(backend, session, subjectName, topicName)` goes into
  **`session.SessionHolder`** (in memory, `AppContainer.sessionHolder`) and the app opens
  `Route.CAPTURE`. A 409 (another session open) is `SessionFailure.Conflict`; a 409 or 404 also
  refreshes the topic list so the session that is really open shows "Continuar".
- Routes: the app now opens on `Route.HOME` when a backend is stored ("Ordenadores" leads to the
  paired backends); `Route.CAPTURE` shows the capture screen (below) for the session in
  `SessionHolder`, and goes back home when there is none.

## Capture screen (#42)

Package `capture`. The screen for one open session: CameraX preview, microphone in the STT mode
the backend picks (ADR-0008), live transcript, pending-doubts counter and the session buttons.

- **`CaptureScreen(viewModel, onLeave, onEnded)`**: asks for `CAMERA` and `RECORD_AUDIO` at runtime
  (Spanish rationale; the session starts once the microphone is granted, the preview once the
  camera is), keeps the screen on (`View.keepScreenOn`) while shown, shows the back camera's
  CameraX `Preview`, the transcript (partials grey, finals black, auto-scrolled), the connection
  state (with "Reintentar" after a failure), "N dudas pendientes" from the last `notice`, and the
  buttons **Capturar**, **Importante**, **Libro/Apuntes** (shows what the camera looks at) and
  **Terminar** (asks for confirmation). Back ("Salir") leaves the session open: the home screen
  offers "Continuar".
- **`CaptureViewModel(open, backendClient, sessionHolder, clock, socketFactory,
  transcriberFactory, audioStreamerFactory, stillCapture)`**, one per session id
  (`AppContainer.captureViewModelFactory(open)`, keyed `capture-<session_id>`), exposes
  `CaptureUiState` (`phase` IDLE/RUNNING/ENDING/ENDED, `connection`, `transcript` -- the last 50
  `TranscriptLine(segmentId, text, final)` from the server's `transcript.partial/final`, so both STT
  modes show the backend's normalised text --, `pendingCount`, `source`, `micProblem`,
  `endFailure`). `start()` opens the socket; when `hello.ack` names the mode it starts the
  `ClientTranscriber` (client mode: each `ClientTranscript` goes out as
  `transcript.client.partial/final` with the transcriber's `provider`/`language`) or the
  `AudioStreamer` (server mode). The mic keeps running through a reconnect. `leave()` stops
  socket and mic. Buttons: `important()` -> `button important`; `toggleSource()` -> `button
  switch_source` with `book`/`notes` (starts on `notes`); `capture()` calls `StillCapture` with
  `trigger: button`; a server `command capture_now` calls it with `trigger: command` and its
  `command_id`, then answers `ack`. `end()` sends `button end_session`, then `POST
  /api/sessions/{id}/end` (`reason: button`); success, 404 or 409 clear the `SessionHolder` and end
  the screen, any other failure keeps the session running with `endFailure` shown.
- **`StillCapture`** (`shots: Flow<List<CaptureShot>>`, `capture(trigger, commandId)`,
  `retry(captureId)`) is what the view model calls; it exposes the strip as
  `CaptureViewModel.shots` and `retryShot(captureId)`. See "Still capture" below.
- **`SessionConnection(scope, socketFactory, url, token, clock, capabilities, resume)`**: the
  protocol v1 socket client. Sends `hello` (`stt: client`, `stt_provider: android-speech`,
  `audio_format` pcm16/16 kHz/mono, since the app can stream), waits for `hello.ack` and exposes
  `state: StateFlow<ConnectionState>` (`Connecting`, `Connected(sttMode, clockOffsetMs)`,
  `Reconnecting(attempt, reason)`, `Failed(ConnectionFailure)`, `Stopped`), `events:
  SharedFlow<ServerEvent>` and `drained: StateFlow<Boolean>`. Reconnects on a drop with back-off
  0.5/1/2/5/10 s, resending from its two backlogs (in memory by default, the disk spool in the
  app; see "Offline spool (#53)"): finals until the backend echoes their `transcript.final`,
  buttons / markers / acks queued while offline, audio frames until the server `ack`'s
  `audio_seq` covers them (renumbered after it when the backend acknowledges more than this
  connection produced). Partials are dropped while offline. A close with 4404 (session not
  active, e.g. the backend restarted) runs `resume` (`POST .../resume`) and reconnects; a refused
  handshake (`HTTP 40x`) is `Failed(Unauthorized)`, a 1008 close or an invalid server message
  `Failed(Refused(reason))`; `retry()` tries again. All state lives on one coroutine fed by a
  channel, so socket callbacks and callers never race.
- **`SessionSocket` / `SessionSocketFactory`**: the socket seam. `OkHttpSessionSocketFactory` opens
  `baseUrl + ws_path` with `Authorization: Bearer <token>` on its own OkHttp client (no read
  timeout, 10 s pings); tests use a scripted fake.
- **`ClientTranscriber`** (ADR-0008's client-side interface: `providerId`, `language`,
  `start(onTranscript, onError)`, `stop()`) and **`SpeechRecognizerTranscriber(engine, clock,
  scope)`**, the default: continuous recognition by chaining one-utterance rounds of a
  `RecognizerEngine`, restarted at once after a result or silence and after 0.25/0.5/1/2/5 s when
  the recognizer fails; `Partial`s and one `Final` per utterance with client timestamps (start at
  the first sign of speech, end at the latest hypothesis); a round that ends without a result
  settles its last partial as the final (also on `stop()`); segment ids `and-<random 8>-<n>`, so
  they stay unique within a session across app restarts; a missing permission or recognizer stops
  it with `TranscriberError`. **`AndroidSpeechRecognizerEngine`** is the `SpeechRecognizer` side:
  `es-ES`, free-form, partial results, `EXTRA_PREFER_OFFLINE`, main thread only. The manifest
  declares `RECORD_AUDIO` and a `<queries>` entry for `android.speech.RecognitionService`
  (package visibility on Android 11+).
- **`AudioStreamer(source, clock, dispatcher)`** (server STT mode): reads an `AudioSource` in 100
  ms frames (1600 samples) off the main thread and hands each to `SessionConnection.sendAudio`
  with the client time of its first sample (the wall clock at the first frame plus the samples
  read since). **`AudioRecordSource`** is the microphone: `AudioRecord`, `VOICE_RECOGNITION`, 16
  kHz mono PCM16. Frames are encoded by `protocol.AudioFrame`.
- **Background pause** (#184): the screen observes its lifecycle; `ON_STOP` calls
  `CaptureViewModel.onBackground()` (skipped on a configuration change, which the view model
  outlives) and `ON_START` `onForeground()`. In the background the transcriber or audio streamer
  stops (an utterance in progress is settled and sent as its final first) while the socket stays
  open with its buffers, so no line is lost or sent twice; a reconnect meanwhile does not restart
  the microphone. Back in the foreground the microphone restarts in the connection's STT mode (a
  new transcriber, so new segment ids; audio `seq` continues on the same connection) and
  `micPaused` shows «Micrófono en pausa» for `PAUSE_NOTICE_MS` (4 s). The camera preview is
  bound to the same lifecycle through CameraX, which closes the camera on `ON_STOP`.

## Still capture (#46)

- **`BurstStillCapture(backend, sessionId, camera, feedback, uploads, clock, scope)`**, built per
  session by `AppContainer.captureViewModelFactory`: on "Capturar" or `capture_now` it calls
  `CaptureFeedback.shutter()` and adds a `CaptureShot` (fresh lowercase UUID `capture_id`, the
  phone time of the trigger, status `CAPTURING`) to the strip at once, then takes a burst of
  `BURST_SIZE` (3) stills with the `StillCamera` and hands it to the `CaptureUploadQueue`. A
  camera that takes nothing marks the shot `CAMERA_FAILED`.
- **`StillCamera`** (`suspend takeBurst(count): Burst`; `Burst(stills, thumbnail)`, `Still(bytes,
  contentType, widthPx, heightPx, clientTimeMs)`, `StillCameraException`) and **`CaptureFeedback`**
  are the seams; `NoStillCamera` / `NoCaptureFeedback` are the container defaults.
  **`CameraXStillCamera(clock)`** (owned by `StudentAssistantApp`, `AppContainer.stillCamera`)
  holds a CameraX `ImageCapture` (`CAPTURE_MODE_MAXIMIZE_QUALITY`, `HIGHEST_AVAILABLE_STRATEGY`,
  flash off) that `CaptureScreen(imageCapture = ...)` binds next to its preview; each still is a
  CameraX-written JPEG with its EXIF orientation, reported with its displayed size; bursts never
  interleave; the thumbnail is the first still at ~256 px. A burst keeps the stills it got when a
  later one fails. **`AndroidCaptureFeedback`**: a 40 ms vibration (`VIBRATE` permission) and
  `MediaActionSound.SHUTTER_CLICK`.
- **`CaptureUploadQueue(scope, client, retryDelaysMs, maxConflictAttempts)`**
  (`AppContainer.captureUploads`, on the container's app-wide `uploadScope`, so uploads go on when
  the capture screen is left): `begin` / `submit` / `cameraFailed` / `retry`, `all` and
  `shots(sessionId)`. Each burst is one `uploadCapture` (parts `image_0..`, the request's
  `client_time_ms` the trigger time, each image its own); uploads run one at a time; the
  `capture_id` never changes, so retries are idempotent (`duplicate` counts as uploaded). Transient
  failures (unreachable, 408/425/429/5xx, an unreadable 2xx, and 409 at most 10 times, since the
  session may be resuming) are retried after 1/2/5/10/30 s (30 s repeats); any other refusal is
  `FAILED`, and a tap on its thumbnail retries it. Statuses: `CAPTURING`, `PENDING`, `UPLOADING`,
  `UPLOADED`, `FAILED`, `CAMERA_FAILED`; full-size stills are dropped once uploaded.
- **Thumbnail strip** (`CaptureScreen`): a row above the transcript, one 64 dp tile per capture
  with a badge (spinner while capturing/uploading, «↑» pending, «✓» sent, «!» failed) and a Spanish
  content description.
- In the app the queue spools every burst to disk before its first upload (see "Offline spool
  (#53)"); a WebSocket `ack`'s `capture_ids` and a resume's `received_capture_ids` mark captures
  uploaded without sending them again.

## Offline spool (#53)

Package `spool`, all under app-private `filesDir/spool` (`Spools(root, budget)`, created by
`AppContainer.spools`):

| what | class | where |
|---|---|---|
| audio frames (server STT mode) not acknowledged | `AudioSpool` (`AudioBacklog`) | `sessions/<session_id>/audio/seg-*.pcm` |
| transcript finals not confirmed, events queued offline | `EventSpool` (`EventBacklog`) | `sessions/<session_id>/events.json` |
| capture bursts not confirmed | `CaptureSpool` | `captures/<capture_id>/{meta.json,image_N,thumbnail}` |
| sessions ended but not yet told to the backend | `PendingEnd` | `ends/<session_id>.json` |

- **`AudioSpool(dir, budget, segmentFrames = 50)`**: each frame (`SpooledFrame(seq, clientTimeMs,
  samples)`) is written through as it is produced into 5 s segment files (`seg-<first>.open`, renamed
  `seg-<first>-<last>.pcm` when full; a record cut short by a crash is truncated away on reopen).
  `after(seq, limit)` reads in `seq` order, `acknowledge(seq)` deletes fully acknowledged segments,
  `rebaseAfter(seq)` renumbers the held frames right after `seq`; a `floor` file keeps numbering
  going after a restart with an empty spool. `MemoryAudioBacklog(600)` is the in-memory default.
- **`EventSpool(file)`**: the finals (at most 200) and queued events (at most 100) as one JSON file,
  rewritten atomically on each change and deleted when empty. `MemoryEventBacklog` is the default.
- **`CaptureSpool(dir, budget)`**: `put(SpooledCaptureMeta(session_id, base_url, request), images,
  thumbnail)` writes `meta.json` last, so an interrupted burst is discarded; `list()`, `images(id)`,
  `thumbnail(id)`, `remove(id)`. The backend is stored by base URL only; its token is looked up in
  the paired backends when the upload resumes. `CaptureUploadQueue(..., spool)` writes each burst
  before its first upload, reads the stills back from disk for each attempt and deletes them once
  uploaded (`stored` or `duplicate`); `restore(credentials)` queues the spooled ones again at app
  start (oldest first; a backend no longer paired leaves them on disk), `confirmReceived(sessionId,
  ids)` marks captures the backend already holds as uploaded without sending them.
- **Cap** (`SpoolBudget(maxBytes)`, `AppContainer(spoolMaxBytes = 512 MiB)`): one byte budget for
  all of the above. From 80 % `nearCap` is true and the capture screen shows «Queda poco espacio
  para guardar sin conexión…» (`capture_spool_near_cap`). Past the cap an audio append deletes that
  session's oldest segments first (acknowledged or not) down to the one being written; captures are
  never dropped to make room.
- **Reconnect order** (`SessionConnection` over the session's spools, on `Dispatchers.IO`): after
  `hello.ack`, queued events go out first; in client mode every unconfirmed final is resent (the
  backend drops a final it already has without echoing it, so a resent final not echoed within 5 s
  of a live connection counts as held); in server mode, with frames held, the connection waits up to
  1 s for the backend's `ack` (sent right after `hello.ack`), then resends every frame after the
  acknowledged `seq` in order, 20 at a time while OkHttp's send queue is under 1 MiB, and only then
  sends live frames (a live frame produced meanwhile joins the backlog). Held frames that do not
  follow the acknowledged `seq` (older audio dropped at the cap) are renumbered after it, since the
  backend places audio by client time and never skips a missing `seq`. After an app restart,
  "Continuar" opens the same spools, so everything left is resent the same way.
- **Captures on resume**: the session start/resume's `received_capture_ids` (the capture screen's
  start and every 4404 resume) are confirmed and failed captures of the session retried.
- **Ending** (`CaptureViewModel` with `CaptureSpooling`): "Terminar" while not connected, or
  answered with a transient failure (unreachable, 408/425/429/5xx), writes a `PendingEnd` and hands
  it to **`SessionFinisher`** (`AppContainer.sessionFinisher`, on the app-wide scope); the screen
  ends at once. Online, "Terminar" first waits up to 10 s for `drained` and the session's uploads.
  The finisher, per pending end: `POST .../resume` (409: skip the flush; 404: drop), a
  `SessionConnection` over the spools until `drained` (at most 120 s), the session's captures
  uploaded (at most 300 s), then `POST .../end` with the original "Terminar" time; success, 404 or
  409 deletes the session's spools and its pending end. Transient failures retry after
  2/5/10/30/60 s; a refusal (401, 403, 400, ...) leaves the pending end on disk.
  `AppContainer.recoverSpool()` (from `StudentAssistantApp.onCreate`) restores the captures and
  resumes the pending ends of an earlier process.
- Known gaps: a session that is neither continued nor ended keeps its spool (and its share of the
  cap) until it is; eviction only drops the audio of the session being recorded; spools are opened
  lazily, possibly on the main thread the first time the capture screen is built; the home screen
  does not show sessions with a pending end, and "Continuar" on one races the finisher.

## Tests
JVM unit tests for view models, protocol (shared examples), spool/retry logic with fakes. The
spool tests (`spool/AudioSpoolTest`, `spool/CaptureSpoolTest`, `capture/SpooledUploadQueueTest`,
`capture/SessionFinisherTest`, `capture/CaptureViewModelOfflineTest`) use a JUnit
`TemporaryFolder`, never `filesDir`. The
capture tests use `capture/Fakes.kt` (test sources): `FakeSessionSocketFactory` (the test plays the
backend: open, receive, drop, close), `FakeRecognizerEngine`, `FakeTranscriber`, `FakeAudioSource`
and `FakeClock`. View-model tests give their `BackendStore` a scope on an
`UnconfinedTestDispatcher`, never `Dispatchers.IO`: store work left on IO outlives the test and
resumes on `Dispatchers.Main` after `MainDispatcherRule` reset it, failing a later `runTest`.

## Build and test
- `bash scripts/test.sh android` runs the JVM unit tests (`./gradlew test` in `android/`);
  `bash scripts/test.sh android assembleDebug` builds the debug APK (extra arguments replace
  `test`). The full log lands in `.cache/test-android-last.log`.
- Toolchain: JDK 17 (`jvmToolchain(17)`, auto-provisioned through the foojay resolver when only a
  newer JDK is on `PATH`), `minSdk` 28, `compileSdk`/`targetSdk` 35, applicationId and namespace
  `com.titanarq.studentassistant`. The Gradle wrapper is committed; there is no system `gradle`.
- Every dependency and plugin version is declared in `android/gradle/libs.versions.toml` and
  nowhere else (the foojay resolver in `settings.gradle.kts` is the one exception, mirrored as
  `foojayResolver` in the catalog because it loads before the catalog does).
- Entry point: `StudentAssistantApp` (the `Application` subclass) creates the single
  `AppContainer`, the manual constructor-DI graph (plain Kotlin, no Android types, members
  `by lazy`); activities read it from the application. New app-wide singletons go there.
- Instrumented tests (`src/androidTest`) are allowed but never run by the test command;
  `connectedAndroidTest`, `adb` and `sdkmanager` are never run by it either.
