# Module: android

**Lives in:** `android/` (`:app`).

## Responsibility
Thin capture client (ADR-0001), Spanish UI:
- Pairing: scan the backend's QR (URL + one-time code), exchange for a token, store it in
  DataStore; several backends allowed; connection test.
- The app is deliberately minimal (#575): capture photos and relate them to subjects and topics.
  Every screen has the same top bar (`ui.AppTopBar` / `AppScaffold`): Back at the left (none on the
  initial screen), the title right after it; the subjects and capture screens also have, at the
  right, the settings gear (opens the computers screen, «Ordenadores pareados») and the user's
  avatar (opens «Editar perfil»).
- Users (#554, #555): «¿Quién eres?» at every start, the chosen user sent as `X-SA-User` on every call;
  «Editar perfil» edits name, email and photo. Back on the subject list signs out (to «¿Quién eres?»).
- Home: subjects/topics from the backend, create topic; a topic card opens the capture screen directly.
- Capture screen: the CameraX preview, the «Capturar» button below it and the thumbnail strip at the
  bottom; entering starts the session, leaving ends it. The microphone and its transcription
  (SpeechRecognizer, or AudioRecord PCM16 in server STT mode, ADR-0008) keep running and being sent
  with no UI; screen kept on.
- Still capture: burst of 3 full-resolution photos on «Capturar» only (#556: the server's
  `capture_now` command is ignored); haptic + shutter sound; upload with retries; thumbnail strip
  (see "Still capture (#46)").
- Share target: "Compartir -> Student Assistant" saves a shared link as a web source of a topic
  (see "Share a web page (#62)").
- Offline resilience: disk spool of audio, transcript lines, session events and photos while
  disconnected, resent in order on reconnect; an end while offline is completed later (see
  "Offline spool (#53)").

## Boundaries
- No study logic, no LLM calls, no vault access. Talks only the `protocol` contract.

## Backend client and pairing (#33)

- **`backend.BackendClient`** (interface) covers every REST endpoint of protocol v1: `pair`,
  `health`, `listSubjects`/`createSubject`, `listTopics`/`createTopic`,
  `startSession`/`resumeSession`/`endSession`, `addWebPage` (#62), the users' `updateUser` / `putUserPhoto` / `deleteUserPhoto` (#555, see "Users") and `uploadCapture` (multipart: a `metadata` JSON
  part plus `image_N` parts, typed by each `CaptureImage.content_type`). Authenticated calls take
  `BackendCredentials(baseUrl, token, userId)` (see "Users (#554)") and send `Authorization: Bearer <token>`; `pair` and
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
  `GET /api/subjects` as the selected user or `GET /api/users` when none is selected, each outcome shown). `AppContainer(filesDir, deviceName)`
  holds `backendClient`, `backendStore` and the three view-model factories; `MainActivity`
  switches between the screens (`ui.Route`) and opens pairing when no backend is stored.
- **Cleartext**: the backend serves plain HTTP on the LAN, and its address is only known at
  pairing time, so `res/xml/network_security_config.xml` permits cleartext for every host (system
  CAs only). The manifest declares `CAMERA` and `INTERNET`.
- Known gap: the backend's `GET /api/health` still answers `{status, version}`, not v1's
  `rest.health.response`, so the health check reports `InvalidResponse` until the server conforms.

## Look, session end on leave and thumbnails (#583)

- **Theme**: `ui.Theme` derives the Material 3 light and dark colour schemes from the web design
  tokens (`web/src/styles/tokens.css`, #296/#299; `ui.DesignTokens` holds the same hex values and
  `DesignTokensTest` fails when they differ from the CSS). The top bar is the web's header band:
  `--header-bg` / `--on-header` as `primaryContainer` / `onPrimaryContainer` (title, Back and
  action icons use the on-colour); the status bar icons are light over it. Buttons use `--accent`,
  the page `--paper`, cards `--surface`, errors `--correction`. The user avatar uses
  `secondaryContainer` (`--accent-soft`) so it stands out on the bar.
- **Leaving capture ends the session reliably.** Root cause of «Hay otra sesión abierta»: the
  end ran in the capture view model's scope (after waiting for the connection to drain), which is
  cancelled the moment the screen is left, so `POST .../end` often never left the phone, and the
  next «start» met the still-open session (409 `session_open`). Now `endOnLeave()` (Back, system
  back, gear, avatar) writes a `PendingEnd` to disk and hands it to the app-wide
  `SessionFinisher` (uploads scope, retried until the backend answers; it flushes the spool and
  confirms the captures first); `onCleared()` does the same for a view model dropped while
  running. Starting a session (`HomeViewModel.startOrContinue`, topic without an open session)
  first waits up to 10 s for `PendingEnds.awaitIdle`; a start answered 409 lists the topics and,
  when this topic has an open session, resumes it silently (`adoptOpen`); an open session of
  another topic (current subject first, then the others) is ended silently and the start is
  retried exactly once (`closeOtherAndRetry`, #589); the conflict message shows only when that
  end or the retry fails. The app process being killed mid-capture
  cannot end the session: the backend's idle auto-end closes it, and the next entry to that
  topic adopts it if it is still open.
- **`capture.ThumbnailStore`** (`AppContainer.thumbnailStore`): in-memory thumbnails keyed by
  `ThumbnailKey(subjectId, topicId)` with `add` (replaces the same capture id), `remove`,
  `replace`, `list`, `flow` and `clear`; at most `MAX_PER_TOPIC` (40) per topic, only the
  downscaled JPEG (about 256 px) is kept. `CaptureViewModel` mirrors its session's shots into it
  and `shots` shows the earlier ones of the topic first, so re-entering capture does not show an
  empty strip. Cleared whenever the selected user changes (profile switch, sign-out, backend
  switch). The future «delete by dragging down» task calls `remove`; `add` keeps the list
  ordered by photo time. A photo whose thumbnail is made
  after the screen was left is not mirrored.

## Users (#554, epic #544)

Several students share one backend and one vault (protocol 1.8, `docs/modules/server.md`). A
device pairs with the backend, not with a user; the app asks «¿Quién eres?» and then acts for the
chosen user. Selecting a user is not authentication (ADR-0001's bearer trust is unchanged).

- **`users.UserHolder`** (on `AppContainer.userHolder`): the selected `User`, in memory only, never
  written to DataStore or any file. It is empty at every process start, after «Cerrar sesión», and
  after the active backend changes (`PairedBackendsViewModel.setActive` / `remove` of the active one,
  and a new pairing, clear it). `clearIfSelected(id)` clears only when `id` is still the selected user.
- **`users.UsersViewModel` / `UserSelectionScreen`** («¿Quién eres?», `Route.USERS`): the active
  backend's users from `BackendClient.listUsers` (`GET /api/users`, sent without `X-SA-User`), each
  row with photo or initials, name and email. A failure shows the existing Spanish backend message
  with «Reintentar»; no users says «No hay usuarios en este ordenador.». The top bar's gear leads to
  the computers screen. The photo is `BackendClient.userPhoto(backend, photo_url)` (`GET` with the bearer
  token, same origin only), cached in memory by `users.UserPhotos` (`invalidate()` for #555);
  `UserAvatar` draws it in a circle or `initialsOf(name)`, and loads it again whenever `UserPhotos.version`
  grows (`invalidate()`, called after a photo change).
- **Routing** (`ui/Route.kt`, tested without Compose): `startRoute(stored, user)` is `PAIRING` with no
  backend, else `USERS` without a user, else `HOME`; `routeFor(route, hasBackends, user)` sends any
  screen of `USER_SCOPED_ROUTES` (home, capture, profile) to `USERS` while a backend is
  stored and no user is selected. `MainActivity` applies it on every change of route or user, so
  sign-out, a backend switch and a refused user all end on the selection. Back does not leave `USERS`
  for the home.
- **Header**: `BackendCredentials(baseUrl, token, userId)` (`forUser(id)`); every user-scoped call of
  `OkHttpBackendClient` (subjects, topics, sessions, captures, web pages) 
  sends `X-SA-User: <id>` (`USER_HEADER`) when `userId` is not null. `pair`, `health`, `listUsers`
  and `userPhoto` never send it. `SessionSocketFactory.open(url, token, userId, listener)` sends it
  on the WebSocket handshake (`SessionConnection(..., userId)`). The view models take the user from
  `UserHolder` when they read the active backend (`HomeViewModel`, `ShareViewModel`,
  `ConnectionTestViewModel`); a user change reloads the home.
  The connection test (#568) runs `GET /api/health`, then one authenticated check with the stored
  token: «asignaturas» (`GET /api/subjects`) as the selected user, or, while nobody is selected
  (right after pairing), «usuarios» (`GET /api/users`, user-independent), so a vault with several
  users never answers it `400 user_required`. No `X-SA-User` is sent without a selected user.
- **Profile (#555)**: `users.ProfileViewModel` / `ProfileScreen` (`Route.PROFILE`) edit the selected
  user. Fields «Nombre» (required, at most `USER_NAME_MAX_CHARS`) and «Correo electrónico»
  (optional, at most `USER_EMAIL_MAX_CHARS`; empty clears it) are trimmed and checked by
  `validateProfile` with the protocol's rules (`ProfileFieldError`, Spanish messages in the screen);
  «Guardar» sends `PATCH /api/users/{id}` with only the fields that differ from the saved user (a
  cleared email travels as `""`; nothing changed sends nothing). Success replaces the selection in
  `UserHolder` with the user the backend answered and returns home with the toast «Perfil guardado»;
  a 422 shows «El ordenador no aceptó estos datos…», any other failure the usual Spanish backend
  message. The user id is never shown or editable. These calls (`BackendClient.updateUser`,
  `putUserPhoto(credentials, userId, bytes, contentType)` raw body with its own `Content-Type`,
  `deleteUserPhoto`; each a `BackendResult<User>`, scripted in `FakeBackendClient`) are not
  user-scoped and send no `X-SA-User`.
  - **Photo**: «Cambiar foto» opens the Photo Picker (`PickVisualMedia.ImageOnly`, no storage
    permission). `PhotoPreparer` decodes the picked image (sampled with `photoSampleSize`), rotates
    it by its EXIF orientation (`exifTransform`), scales it to at most `PHOTO_MAX_EDGE_PX` (1024) on
    the long edge (`photoTargetSize(width, height, exifOrientation)`, never enlarging) and encodes
    JPEG quality 85 on `Dispatchers.Default`, so the upload stays far under the 5 MiB cap
    (`PHOTO_MAX_BYTES`, checked again before sending; an unreadable or larger image shows «No se
    pudo leer esa imagen»). «Quitar foto» (shown only when the user has one) asks for confirmation
    and sends `DELETE`. After either, `UserPhotos.invalidate()` makes the home bar and the selection
    screen draw the new `photo_url`.
- **Share**: `ShareActivity` shows «¿Quién eres?» before its subjects while no user is selected
  (same `UserHolder`, same process).
- **Top bar** (#575): the user's avatar at the right (content description «Perfil de <nombre>») opens
  «Editar perfil» (`Route.PROFILE`); there is no menu and no «Cerrar sesión» button any more: Back on
  the subject list (`HomeViewModel.signOut()` clears `UserHolder` and `SessionHolder`) returns to
  «¿Quién eres?». An open capture session stays open on the backend and its spool keeps its
  user, so it can be continued or ended later.
- **`user_required` / `user_not_found`**: `BackendResult.HttpError(status, code)` carries `code` only
  for these two (`400 user_required`, `404 user_not_found`; `userRejected`); no other error body is
  read. `AppContainer.scopedBackendClient`
  (`UserRejectionBackendClient`) wraps the client used by the screens
  and the background workers: such a refusal of a call made as the selected user clears `UserHolder`
  and the app opens the selection. A refusal of a spooled item sent as another user (or none) changes
  nothing.
- **Offline spool**: `SpooledCaptureMeta`, `PendingEnd` and the `session.json` written by
  `Spools.bind(sessionId, baseUrl, userId)` store `user_id` (`Spools.userOf`). `CaptureUploadQueue.restore`,
  `SessionFinisher` (`restore`, `continueInstead`) and `StaleSpoolSweeper` send each item's own user,
  never the one selected now. Items written before this version have no `user_id`: they are sent
  without the header (the backend's single-user fallback) and still decode. The sweeper reads each
  backend once per user.

## Home: subjects, topics and sessions (#37)

- **`home.HomeViewModel(client, store, sessions, clock)`** drives the home screen on the active
  backend, exposing `HomeUiState`: `subjects` and the selected subject's `topics` as
  `Loadable` (`Loading`/`Loaded`/`Failed(BackendResult.Failure)`), the create-topic dialog
  (`CreateTopicDialog(saving, failure)`, null when closed), `session: SessionAction`
  (`Idle`/`Opening(topicId)`/`Failed(topicId, SessionFailure)`) and `openedSession`, a one-shot
  the screen consumes with `onSessionShown()` to navigate. `load()` runs every time the home is
  shown (a switched backend resets the state; the selected subject is kept while it exists).
- Topic rows are `TopicRow(topic, lastSessionAtMs?, ending)`; `canContinue` is
  `topic.open_session_id != null`. `lastSessionAtMs` is the topic's `last_session_at_ms` (protocol
  1.1), shown as the date of the last capture (`formatCaptureDate`, date only) when present. The
  topic card (#575) is the whole card as one button: the name, the date below it, and a capture icon
  at the bottom-right; nothing else (no digest excerpt, doubts count, session state or buttons).
  Tapping it calls `startOrContinue(row)` and the capture screen shows.
- **No connection** (#575): `HomeUiState.noConnection` is true with no stored backend or when the
  subject or topic list failed as `BackendResult.Unreachable`; the screen then shows only a warning
  («No hay conexión con el ordenador…») with «Configurar conexión» (the computers screen) and
  «Reintentar». There is no other connectivity feedback in the app (no address, no
  connected/disconnected indicator).
- **Create topic** (`createTopic(subjectName, title)`): the dialog takes a subject name (typed, or
  one tap on an existing one) and a title. A name matching an existing subject (trimmed, ignoring
  case) reuses it; otherwise `POST /api/subjects` runs first. Then `POST .../topics`, the dialog
  closes and that subject's topics are shown. Failures stay in the dialog.
- **Opening a topic** (`startOrContinue(row)`): `POST /api/sessions` with the
  clock's `client_time_ms`, or `POST /api/sessions/{open_session_id}/resume`. On success the
  `session.OpenSession(backend, session, subjectName, topicName)` goes into
  **`session.SessionHolder`** (in memory, `AppContainer.sessionHolder`) and the app opens
  `Route.CAPTURE`. A 409 (another session open) is `SessionFailure.Conflict`; a 409 or 404 also
  refreshes the topic list so the session that is really open is the one resumed.
- **Pending ends** (#198): `HomeViewModel(..., pendingEnds)` takes a `session.PendingEnds` (the
  `SessionFinisher` in the app, `NoPendingEnds` by default). A row whose `open_session_id` is in
  `pending` has `TopicRow.ending` and is only used to stop that end first; when a
  session leaves `pending` the topics are fetched again. Opening a topic with an open session always resumes through
  `PendingEnds.continueInstead(sessionId) { resume }`: the finisher's attempt in flight is cancelled
  and joined first (its flush socket is let go), so no end races the resume; a successful resume
  drops the pending end for good (also one refused earlier, so a later start never ends a session
  in use), a failed one lets a running end go on. An end the backend already took shows up as a 409
  on the resume (the conflict message, the list refreshed).
- Routes: `Route.HOME` is the subjects/topics screen (the gear leads to the paired backends, whose
  Back returns to the screen that opened it, `backRoute`); `Route.CAPTURE` shows the capture screen (below) for the session in
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

## Capture screen (#42)

Package `capture`. The screen for one open session (#575), nothing but the capture: the shared top
bar (Back, «<asignatura> · <tema>», gear, avatar), the CameraX preview (the only weighted child, so it
takes the vertical space left), **Capturar** BELOW the preview (a full-width button outside it; it was
overlaid and did not respond because it stayed disabled until the microphone permission started the
session), and at the bottom the thumbnail strip. The microphone runs in the STT mode the backend picks
(ADR-0008) and its transcription is sent, but the screen shows neither it, nor the connection, the
pending doubts, nor any warning (the plumbing stays: a hidden UI, not a protocol change).

- **`CaptureScreen(viewModel, user, photos, onLeave, onSettings, onProfile)`**: keeps the screen on
  (`View.keepScreenOn`); entering calls `start()` at once (the session runs even if the microphone is
  refused, then without transcript) and asks for `CAMERA` and `RECORD_AUDIO` once; the preview shows
  as soon as the camera is granted. **Leaving ends the session**: the top bar's Back, the system
  back, the gear and the avatar call `CaptureViewModel.endOnLeave()` first. There are no Start/End
  buttons, no «Importante», no «Libro/Apuntes» and no «Sesión terminada» panel.
- **Ending** (#431, #575): nothing anywhere builds the topic's document automatically.
  `CaptureViewModel.endOnLeave()` sends `button end_session` and `POST /api/sessions/{id}/end`
  **without `prepare_notes`**; a spooled end is delivered without it too (see "Offline spool"). The
  session is released from `SessionHolder` as soon as the end is taken (live, 404/409 already ended,
  or handed to the spool) or refused for a non-transient reason (the socket stops and the backend
  closes the session on its own later); the screen can already be gone by then. A screen that never
  started the session (`IDLE`) ends it too. The former «Terminar y preparar apuntes» button (#272),
  its `NOTES` phase, `NotesProgress` / `NotesGenerationPoller` and `BackendClient.notesGeneration`
  are gone; the protocol mirror keeps `SessionEndRequest.prepareNotes` (never sent).
- **`CaptureViewModel(open, backendClient, sessionHolder, clock, socketFactory,
  transcriberFactory, audioStreamerFactory, stillCapture)`**, one per session id
  (`AppContainer.captureViewModelFactory(open)`, keyed `capture-<session_id>`), exposes
  `CaptureUiState` (`phase` IDLE/RUNNING/ENDING/ENDED, `connection`, `transcript` -- the last 50
  `TranscriptLine(segmentId, text, final)` from the server's `transcript.partial/final`, so the
  state holds the backend's normalised text in both STT modes, though since #556 nothing draws it
  --, `pendingCount`, `micProblem`, `endFailure`, `sttWarning`: the last degraded
  `SttStatus`, cleared by an `ok` one and by every
  new `hello.ack`, after which the backend repeats a status that still holds). `start()` opens the socket; when `hello.ack` names the mode it starts the
  `ClientTranscriber` (client mode: each `ClientTranscript` goes out as
  `transcript.client.partial/final` with the transcriber's `provider`/`language`) or the
  `AudioStreamer` (server mode). The mic keeps running through a reconnect. `leave()` stops
  socket and mic. `capture()` calls `StillCapture` with
  `trigger: button` and is the app's only capture trigger (#556): a server `command capture_now`
  -- the voice command the backend's `stt` still publishes -- is deliberately neither acted on nor
  `ack`ed (nothing was done, so nothing is acknowledged), and every other server event is handled
  as before. The protocol is unchanged: `CaptureTrigger.COMMAND`, `Command`/`CommandName` and
  `ClientAck` stay in the protocol package and the web capture page still obeys `capture_now`
  (docs/modules/web.md). `end()` (called by `endOnLeave()`) sends `button end_session`, then `POST
  /api/sessions/{id}/end` (`reason: button`). `important()` / `toggleSource()` and the `source` state
  were removed with their buttons (#575); the protocol's `ButtonName.IMPORTANT` / `SWITCH_SOURCE`
  stay in the protocol package.
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
  `micPaused` shows «Micrófono en pausa» for `PAUSE_NOTICE_MS` (4 s). Since #425
  `onBackground()` also sends `button: pause` and `onForeground()` `button: resume` over the
  session socket, and a reconnect in the background says `pause` again after its `hello.ack`, so
  the backend knows the app stopped sending (and ends the session itself after `[server]
  capture_idle_end_seconds` with nothing sending). The camera preview is
  bound to the same lifecycle through CameraX, which closes the camera on `ON_STOP`.

## Still capture (#46)

- **`BurstStillCapture(backend, sessionId, camera, feedback, uploads, clock, scope)`**, built per
  session by `AppContainer.captureViewModelFactory`: on "Capturar" -- the app's only trigger since
  #556; `capture(trigger, commandId)` still takes the protocol's `CaptureTrigger.COMMAND` and its
  `command_id`, which only the tests pass now -- it calls `CaptureFeedback.shutter()` and adds a
  `CaptureShot` (fresh lowercase UUID `capture_id`, the phone time of the trigger, status
  `CAPTURING`) to the strip at once, then takes a burst of `BURST_SIZE` (3) stills with the
  `StillCamera` and hands it to the `CaptureUploadQueue`. A camera that takes nothing marks the
  shot `CAMERA_FAILED`.
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
- **Thumbnail strip** (`CaptureScreen`): a row at the bottom of the screen, below the «Capturar»
  button (#575); it shows the topic's earlier photos plus the session's own, newest last (#580:
  `capture.EarlierCaptures` lists `GET .../topics/{t}/captures` through `BackendClient.listTopicCaptures`,
  downloads each missing 256 px thumbnail with `captureThumbnail` as the active user and `add`s it to
  the `ThumbnailStore` as uploaded; best effort, a failure keeps what the process already has), each its own keyed `Thumbnail` composable so a later task can add
  discarding by dragging a thumbnail down (not implemented). One 72 dp tile per capture with a badge (spinner while capturing/uploading, «↑» pending, «✓»
  sent, «!» failed) and a Spanish content description.
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
  Resuming the session opens the same spools, so everything left is resent the same way.
- **Captures on resume**: the session start/resume's `received_capture_ids` (the capture screen's
  start and every 4404 resume) are confirmed and failed captures of the session retried.
- **Ending** (`CaptureViewModel` with `CaptureSpooling`): Ending while not connected, or
  answered with a transient failure (unreachable, 408/425/429/5xx), writes a `PendingEnd` and hands
  it to **`SessionFinisher`** (`AppContainer.sessionFinisher`, on the app-wide scope); the screen
  ends the screen's work at once. Online, ending first waits up to 10 s for `drained` and the session's uploads.
  The finisher, per pending end: `POST .../resume` (409: skip the flush; 404: drop), a
  `SessionConnection` over the spools until `drained` (at most 120 s), the session's captures
  uploaded (at most 300 s), then `POST .../end` with the original «Terminar captura» time and
  never `prepare_notes` (#431): a `PendingEnd` spooled by an older version with
  `prepare_notes: true` still decodes (`PendingEnd.prepareNotes`, read only) and is sent without
  it; success, 404 or
  409 deletes the session's spools and its pending end. Transient failures retry after
  2/5/10/30/60 s; a refusal (401, 403, 400, ...) leaves the pending end on disk.
  `AppContainer.recoverSpool()` (from `StudentAssistantApp.onCreate`) restores the captures and
  resumes the pending ends of an earlier process.
- Known gaps: spools are opened lazily, possibly on the main thread the first time the home or
  capture screen is built; the stale sweep runs only at app start, and data of a backend that is no
  longer paired is never swept.

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
