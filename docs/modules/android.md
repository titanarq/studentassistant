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
