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
  sound; upload with retries; thumbnail strip.
- Offline resilience: local spool of audio and photos while disconnected, resumed by `seq`.

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
- **`StillCapture`** (`fun interface`, `capture(trigger, commandId)`) is the hook for android
  still capture (#46); `AppContainer.stillCapture` defaults to `NoStillCapture`, which does nothing.
- **`SessionConnection(scope, socketFactory, url, token, clock, capabilities, resume)`**: the
  protocol v1 socket client. Sends `hello` (`stt: client`, `stt_provider: android-speech`,
  `audio_format` pcm16/16 kHz/mono, since the app can stream), waits for `hello.ack` and exposes
  `state: StateFlow<ConnectionState>` (`Connecting`, `Connected(sttMode, clockOffsetMs)`,
  `Reconnecting(attempt, reason)`, `Failed(ConnectionFailure)`, `Stopped`) and `events:
  SharedFlow<ServerEvent>`. Reconnects on a drop with back-off 0.5/1/2/5/10 s, resending from
  in-memory buffers (the disk spool is android-offline): finals until the backend echoes their
  `transcript.final` (same `segment_id`; the backend ignores a final it already has), buttons /
  markers / acks queued while offline (at most 100), audio frames until the server `ack`'s
  `audio_seq` covers them (at most 600, ~60 s; when the backend already acknowledges more frames
  than this connection produced -- a restarted app on a resumed session -- the buffered frames are
  renumbered after that `seq`). Partials are dropped while offline. A close with 4404 (session not
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
- Known gap: nothing here is spooled to disk (android-offline).

## Tests
JVM unit tests for view models, protocol (shared examples), spool/retry logic with fakes. The
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
