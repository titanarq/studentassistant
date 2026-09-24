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

## Tests
JVM unit tests for view models, protocol (shared examples), spool/retry logic with fakes.

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
