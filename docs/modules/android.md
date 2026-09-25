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
- Share target: "Compartir -> Student Assistant" saves a shared link as a web source of a topic
  (see "Share a web page (#62)").
- Study desk (#83): a topic's notes and the editor chat, the backend's web UI in a WebView.
- Voice tutor (#248): «Preguntar al tutor» on a topic, a spoken or typed question answered from the
  notes and sources by the backend, read aloud with `TextToSpeech`.
- Offline resilience: disk spool of audio, transcript lines, session events and photos while
  disconnected, resent in order on reconnect; an end while offline is completed later (see
  "Offline spool (#53)").

## Boundaries
- No study logic, no LLM calls, no vault access. Talks only the `protocol` contract.

## Backend client and pairing (#33)

- **`backend.BackendClient`** (interface) covers every REST endpoint of protocol v1: `pair`,
  `health`, `listSubjects`/`createSubject`, `listTopics`/`createTopic`,
  `startSession`/`resumeSession`/`endSession`, `addWebPage` (#62) and `uploadCapture` (multipart: a `metadata` JSON
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
- Topic rows are `TopicRow(topic, lastSessionAtMs?, pendingCount?, digestExcerpt?)`;
  `canContinue` is `topic.open_session_id != null`. `lastSessionAtMs` / `pendingCount` are the
  topic's `last_session_at_ms` / `pending_count` (protocol 1.1) and `digestExcerpt` its
  `digest_excerpt` (1.3, the topic digest's summary: where the topic was left). The excerpt is
  shown under the topic name (at most three lines), the date and doubts count below; each only
  when present (an older backend leaves the newer ones out).
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
- **Pending ends** (#198): `HomeViewModel(..., pendingEnds)` takes a `session.PendingEnds` (the
  `SessionFinisher` in the app, `NoPendingEnds` by default). A row whose `open_session_id` is in
  `pending` has `TopicRow.ending` and shows «Terminando sesión…» instead of «Sesión abierta»; when a
  session leaves `pending` the topics are fetched again. "Continuar" always resumes through
  `PendingEnds.continueInstead(sessionId) { resume }`: the finisher's attempt in flight is cancelled
  and joined first (its flush socket is let go), so no end races the resume; a successful resume
  drops the pending end for good (also one refused earlier, so a later start never ends a session
  in use), a failed one lets a running end go on. An end the backend already took shows up as a 409
  on the resume (the conflict message, the list refreshed).
- Routes: the app now opens on `Route.HOME` when a backend is stored ("Ordenadores" leads to the
  paired backends); `Route.CAPTURE` shows the capture screen (below) for the session in
  `SessionHolder`, and goes back home when there is none.

## Share a web page (#62)

- **`share.ShareActivity`** (exported, `ACTION_SEND` + `text/plain`, label «Guardar en un tema»)
  is the app's entry in the system share sheet. It reads `EXTRA_TEXT` / `EXTRA_SUBJECT` and shows
  `share.ShareScreen` on its own (it never opens the main navigation); «Cerrar» / «Cancelar»
  finish it.
- **`share.extractSharedUrl(text, subject)`** takes the first `http(s)://` link of the text (a
  browser often puts the page title first), else of the subject, leaving out punctuation glued
  to its end (`.`, `,`, `»`, quotes, ...; a `)` only when it does not close a `(` of the link);
  null when there is none or it is longer than `MAX_SHARED_URL_LENGTH` (2000, the protocol's).
- **`share.ShareViewModel(sharedText, sharedSubject, client, store)`**
  (`AppContainer.shareViewModelFactory`): without a link nothing is called («Lo compartido no
  contiene ninguna dirección web»); without a paired backend it says so. Otherwise the active
  backend's subjects, then the picked subject's topics (the one with an open session first, the
  likely target), each with «Guardar aquí». A pick calls `BackendClient.addWebPage` (`POST
  /api/subjects/{s}/topics/{t}/web-pages`, `rest.topics.web_pages.create.request` with `via:
  share`); the backend fetches and stores the page -- the phone only carries the address
  (ADR-0001). `ShareUiState.outcome` is `Saved(topic, title, alreadyKept)` («Guardada «<título>»
  como fuente del tema «<tema>»», or that the topic already had it) or `Failed(topic, failure)`,
  after which another topic can be picked. Error bodies are not read: 404 (unknown topic, or a
  backend without this endpoint), 409 (cost cap), 422 (not a text page / not fetched), 502
  (Claude failed) and 503 (web tools off) have their own Spanish messages.
- `OkHttpBackendClient.addWebPage` uses a client with a longer read timeout
  (`WEB_PAGE_READ_TIMEOUT_SECONDS`, 120 s): the backend answers after Claude fetched the page.

## Study desk on the phone (#83)

Package `desk`. The phone reads a topic's notes and talks to the editor through the backend's own
web UI (the notes viewer with the editor chat beside it, web #52/#71): no notes logic in the app
(ADR-0001).

- **«Apuntes»** on every topic card of the home screen opens `Route.DESK` for that
  `DeskTopic(subjectId, topicId, topicName)` (kept by `MainActivity` across recreation).
- **`StudyDesk.kt`** (pure, JVM-tested): `notesPageUrl(baseUrl, subjectId, topicId)` ->
  `<base>/subjects/<s>/topics/<t>/notes` (each id percent-encoded as one segment, the base URL's
  path/query dropped; null for a non-http(s) base), `backendOrigin(baseUrl)`,
  `tokenCookie(token)` -> `sa_token=<token>; Path=/; HttpOnly; SameSite=Strict`,
  `deskPage(baseUrl, token, topic)` -> `DeskPage(url, cookieUrl, cookie)` (its `toString()` hides
  the cookie) and `isSameOrigin(url, baseUrl)` (scheme, host and port).
- **Authentication**: a page cannot send `Authorization: Bearer` on its own loads and `fetch`
  calls, so the backend also accepts the paired token from the `sa_token` cookie (server
  `auth.TOKEN_COOKIE`, docs/modules/server.md). The screen sets it in the WebView `CookieManager`
  for the backend's origin right before loading, and removes the WebView's cookies (then
  flushes) when the screen is left, so the token does not stay in the WebView store. It is
  never logged or put in a URL.
- **`StudyDeskViewModel(store, topic)`** (`AppContainer.studyDeskViewModelFactory(topic)`, keyed
  `desk-<subject>/<topic>`) reads the active backend once: `DeskUiState` `Loading`, `NoBackend`,
  `InvalidBackend` or `Ready(backendName, baseUrl, page, reload, failure)`.
  `onLoadFailed(status, detail)` records a main-frame failure (`401` ->
  `DeskLoadFailure.Unauthorized`, «Vuelve a emparejarlo»; else `Failed("HTTP <n>" | detail)`);
  `retry()` («Reintentar», «Recargar») clears it and bumps `reload`.
- **`StudyDeskScreen`**: a bar with «Volver», «Apuntes de <tema>» and «Recargar» over the
  WebView (JavaScript and DOM storage on, file/content access off). Links to another origin open
  in the system browser; system back goes back in the WebView history, then home. A progress bar
  shows while a page loads; a failure covers the page with its Spanish message.
- **Files and downloads (#259)**: a file input (the topic's PDF upload) opens the system document
  picker (`OpenDocument`, or `OpenMultipleDocuments` for a `multiple` input) filtered by
  `acceptMimeTypes(accept)`; a cancel answers `null`, so the input stays usable. A download (the
  generated materials' links: Anki deck, PDF exam, slides) goes through `decideDownload` and then
  `DownloadManager` with `Authorization: Bearer <token>` (never logged; `DeskPage.token`,
  redacted from `toString()`), the server's file name (`URLUtil.guessFileName` on
  `Content-Disposition`, made safe by `safeFileName`) and a notification, into Downloads (the
  app's own external Downloads folder on Android 9, which would need a storage permission for
  the shared one); a URL that is not on the paired backend's origin is refused. Toasts:
  «Descarga iniciada: <archivo>», «No se pudo descargar el archivo.», «Solo se descargan
  archivos del ordenador emparejado.». The decisions live in `DeskFiles.kt` (pure, JVM-tested),
  the Android glue in `DeskDownloader.kt`.
- Known gaps: the web layout is the desktop one (it wraps below 80rem, the chat under the notes);
  no microphone inside the WebView (the voice tutor is native, #248); nothing offline.

## Voice tutor on the phone (#248)

Package `tutor`. The phone side of the backend's voice tutor (#82, `server/tutor_routes.py`,
docs/modules/server.md and editor.md "The voice tutor"): the backend answers, grounded in the
topic's notes and sources; the app only carries the question and shows / reads the answer
(ADR-0001). It is the web capture page's tutor (docs/modules/web.md) in native Compose.

- **«Preguntar al tutor»** on every topic card of the home screen opens `Route.TUTOR` for that
  `TutorTopic(subjectId, topicId, topicName)` (kept by `MainActivity` across recreation).
- **`TutorClient`** (`history`, `ask`) over the active backend's `BackendCredentials` (bearer
  token); **`OkHttpTutorClient`** is the real one (read timeout `TUTOR_READ_TIMEOUT_SECONDS`, 180 s,
  since Claude reads before the first delta; cancelled with the coroutine; nothing logged). Not part
  of the capture protocol: bodies are read leniently (`TutorJson`, unknown fields ignored).
  - `GET /api/subjects/{s}/topics/{t}/tutor` -> the `turns` (`TutorTurn`: `time`, `question`,
    `reply`, `refs`, `warning`), oldest first.
  - `POST .../tutor` `{"question", "confirm_over_cap"}` with `Accept: text/event-stream`: the
    stream is read by **`readTutorStream(source, onProgress)`** over **`SseParser`** (event name +
    joined `data:` lines; comments, `id:`, `retry:` ignored): `reply.delta` -> `TutorProgress.Delta`,
    `reply.restart` -> `TutorProgress.Restart`, `result` -> the `TutorAnswer` (`question`, `reply`,
    `refs` `{label, kind, text, source_id, path}`, `warning`), `error` -> a refusal with the event's
    own status.
  - Every call returns a `TutorResult`: `Success`, `Refused(status, detail, code)` (an HTTP error
    before the stream or an `error` event; `detail` only when it is a string; `overCap` when `code`
    is `cost_cap_reached`), `Unreachable(reason)`, `Interrupted` (the stream ended or broke before
    `result`/`error`) or `InvalidResponse(reason)`.
- **`VoiceQuestion(engine)`**: one listening round of the capture screen's `RecognizerEngine`
  (`AndroidSpeechRecognizerEngine`, `es-ES`, one per tutor screen), not continuous: `onInterim`
  while the student speaks, then exactly one `onFinal(text)` or `onProblem(VoiceProblem)`
  (`NO_SPEECH`, `PERMISSION_DENIED`, `UNAVAILABLE`, `FAILED`). A round that ends in an empty
  result or an error after partials keeps the last partial; `stop()` («Ya he terminado») ends it
  with what was heard; `cancel()` / `release()` report nothing (`release` also destroys the
  recognizer, recreated on the next round).
- **`SpeechOutput`** (`available`, `speak(text, onDone)`, `stop()`, `shutdown()`):
  **`AndroidSpeechOutput`** is `TextToSpeech` in `es-ES` (one per app, `AppContainer.speechOutput`;
  a text asked for before the engine is ready waits for it; no engine or no Spanish makes
  `available` false; texts over the engine's input limit go as several utterances,
  `speechChunks`). `NoSpeechOutput` is the container default. The manifest's `<queries>` lists
  `android.intent.action.TTS_SERVICE` (package visibility on Android 11+).
- **`shownText(reply)`** shows each `[^label]` as `[label]` (matching «Fuentes:»);
  **`spokenText(reply)`** drops the marks, `[[?..]]` brackets and Markdown symbols -- the web's
  `speech.ts` rules.
- **`TutorViewModel(client, store, topic, voice, speech)`**
  (`AppContainer.tutorViewModelFactory(topic)`, keyed `tutor-<subject>/<topic>`) exposes
  `TutorUiState`: `setup` (`Loading`/`NoBackend`/`Ready(backendName)`), `history`
  (`Loading`/`Loaded`/`Failed(failure)`, «Reintentar» -> `retryHistory()`), `turns`, `draft`
  (capped at `MAX_QUESTION_CHARS`, 1000, the backend's), `listening`/`heard`/`voiceProblem`,
  `pending` (`PendingQuestion(question, partial)`, the answer streamed so far), `failure`
  (`AskFailure(question, failure)`), `readAloud` (on by default), `speaking`, `speechAvailable`.
  One question at a time (`canAsk`). `askDraft()` («Preguntar»), `startVoice()` («Preguntar por
  voz»: the recognised question is asked at once), `stopVoice()`; an answer is appended to
  `turns` and, with `readAloud`, read aloud (`spokenText`); `readAgain(turn)` («Leer otra vez»),
  `stopSpeaking()` («Parar de leer»), `setReadAloud(false)` stops the reading. A failure keeps
  its question: `confirmOverCap()` («Continuar igualmente», only for a reached cost cap) asks it
  again with `confirm_over_cap`, `retryFailed()` («Reintentar») without. `onBackground()` stops
  listening (releasing the recognizer) and reading.
- **`TutorScreen`**: «Volver» and «Tutor: <tema>», the earlier turns (question, answer, a
  warning, «Fuentes:» `[label] text`), the question being answered («El tutor está pensando…»
  until the first delta), the failure card, then the controls («Preguntar por voz» / «Lo que te
  oigo: …» + «Ya he terminado», the typed field + «Preguntar», the «Leer las respuestas en voz
  alta» switch with «Parar de leer»). `RECORD_AUDIO` is asked for on the first spoken question; a
  refusal says typing still works. Leaving the screen or `ON_STOP` (not a rotation) calls
  `onBackground()`.
- Messages: `ui.tutorFailureMessage` -- 401 the re-pair message, otherwise the backend's Spanish
  `detail` when it gave one (no notes yet, another question running, cost cap, Claude failed,
  ...), else 404 / 503 / the HTTP status; unreachable, interrupted and invalid answers have their
  own. Each `VoiceProblem` has its Spanish sentence.
- Known gaps: the answer's sources are listed, not opened (the study desk shows them); nothing
  offline.

## Capture screen (#42)

Package `capture`. The screen for one open session: CameraX preview, microphone in the STT mode
the backend picks (ADR-0008), live transcript, pending-doubts counter and the session buttons.

- **`CaptureScreen(viewModel, onLeave, onEnded)`**: asks for `CAMERA` and `RECORD_AUDIO` at runtime
  (Spanish rationale; the session starts once the microphone is granted, the preview once the
  camera is), keeps the screen on (`View.keepScreenOn`) while shown, shows the back camera's
  CameraX `Preview`, the transcript (partials grey, finals black, auto-scrolled), the connection
  state (with "Reintentar" after a failure), "N dudas pendientes" from the last `notice`, in
  server STT mode the backend recognizer's warning while degraded (protocol 1.5 `stt.status`,
  #222: its Spanish `detail`, or `capture_stt_reconnecting` / `capture_stt_unavailable`), and the
  buttons **Capturar**, **Importante**, **Libro/Apuntes** (shows what the camera looks at) and
  **Terminar** (asks for confirmation). Back ("Salir") leaves the session open: the home screen
  offers "Continuar".
- **`CaptureViewModel(open, backendClient, sessionHolder, clock, socketFactory,
  transcriberFactory, audioStreamerFactory, stillCapture)`**, one per session id
  (`AppContainer.captureViewModelFactory(open)`, keyed `capture-<session_id>`), exposes
  `CaptureUiState` (`phase` IDLE/RUNNING/ENDING/ENDED, `connection`, `transcript` -- the last 50
  `TranscriptLine(segmentId, text, final)` from the server's `transcript.partial/final`, so both STT
  modes show the backend's normalised text --, `pendingCount`, `source`, `micProblem`,
  `endFailure`, `sttWarning`: the last degraded `SttStatus`, cleared by an `ok` one and by every
  new `hello.ack`, after which the backend repeats a status that still holds). `start()` opens the socket; when `hello.ack` names the mode it starts the
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
  `vocabularyHints`, `start(onTranscript, onError)`, `stop()`) and **`SpeechRecognizerTranscriber(engine, clock,
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
- **Vocabulary hints** (protocol 1.4, #228): `CaptureViewModel.vocabularyHints` keeps the
  session's latest list -- the one `hello.ack` brings (carried by `ConnectionState.Connected`, so
  it is known before the microphone starts), replaced whenever a `notice` carries
  `vocabulary_hints`; a missing list keeps the current one. It is set on every new transcriber
  and on the running one, which passes it to `RecognizerEngine.startListening(language,
  vocabularyHints, listener)` from its next round (a round in progress is not interrupted).
  `AndroidSpeechRecognizerEngine` puts them in `RecognizerIntent.EXTRA_BIASING_STRINGS` on API
  33+ (`biasingStrings(hints, sdkInt)`); older devices, and recognizers that do not support
  biasing, ignore them.
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
| the backend a session's spool belongs to | `Spools.bind` | `sessions/<session_id>/session.json` |

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
  para guardar sin conexión…» (`capture_spool_near_cap`). Past the cap (#198) `Spools`, the
  budget's evictor, drops whole audio segments (acknowledged or not) of any session, the oldest
  first by the client time of their first frame (`AudioSpool.oldestDroppableTimeMs` /
  `dropOldest`), never a segment being written and never a capture. It runs after every audio
  append and capture write (`SpoolBudget.enforce`, outside the spool's own lock) and once at start,
  so audio left over a lowered cap is trimmed. An `AudioSpool` on a budget with no evictor still
  trims only itself.
- **Stale sessions** (#198, `spool.StaleSpoolSweeper`, `AppContainer(spoolGraceMs = 24 h)`): at app
  start (`recoverSpool`, before captures are queued again) the audio/events of a session and the
  captures untouched for the grace period are deleted when the backend reports the session ended
  or unknown: no topic of `GET /api/subjects` + `GET .../topics` lists it as `open_session_id` (read
  only; a resume would reopen it). The backend asked is the one `Spools.bind` recorded when the
  capture screen opened the session (a capture's own `base_url`); a session with none must be
  absent from every paired backend. Any failed list call, a backend no longer paired, a pending
  end, the session in `SessionHolder` or one bound in this process keeps the data.
- **Reconnect order** (`SessionConnection` over the session's spools, on `Dispatchers.IO`): after
  `hello.ack`, queued events go out first; in client mode every unconfirmed final is resent (the
  backend drops a final it already has without echoing it, so a resent final not echoed within 25 s
  -- more than two 10 s ping intervals, so the socket is proven alive -- counts as held); in server mode, with frames held, the connection waits up to
  1 s for the backend's `ack` (sent right after `hello.ack`), then resends every frame after the
  acknowledged `seq` in order, 20 at a time while OkHttp's send queue is under 1 MiB, and only then
  sends live frames (a live frame produced meanwhile joins the backlog). Frames are renumbered only
  on a real `ack`: when the held frames do not follow its `seq` (older audio dropped at the cap)
  and none past it was sent on this socket, they are renumbered after it, since the backend places
  audio by client time and never skips a missing `seq`. Without an `ack` in time they go out as
  numbered, renumbered from 0 only when the backlog never saw any `ack` (its last acknowledged
  `seq` is kept in the `floor` file) and its first frame is not 0. After an app restart,
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
- Known gaps: spools are opened lazily, possibly on the main thread the first time the home or
  capture screen is built; the stale sweep runs only at app start, and data of a backend that is no
  longer paired is never swept.

## Tests
JVM unit tests for view models, protocol (shared examples), spool/retry logic with fakes. The
tutor tests use `tutor/Fakes.kt` (`FakeTutorClient`, whose questions the test answers through a
`CompletableDeferred`, and `FakeSpeechOutput`) with the capture tests' `FakeRecognizerEngine`, and
MockWebServer for `OkHttpTutorClient`. The
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
