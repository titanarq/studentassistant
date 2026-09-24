# ADR-0008: Speech-to-text is a pluggable provider

Status: accepted (2026-09-24)

## Context
Local Whisper needs a GPU and adds latency and setup; Google's recognizers are already on the
capture devices (Chrome's Web Speech API, Android's SpeechRecognizer) and are good at Spanish.
Other providers may be needed later.

## Decision
- STT is chosen by config: `stt.mode = client|server` and `stt.provider`.
- **Default `client`**: the capture client transcribes with Google (web capture page: Web Speech
  API `es-ES`, continuous, interim results; Android: SpeechRecognizer) and sends
  `transcript.client.partial/final` segments with client timestamps. No audio goes to the backend.
- **`server`**: the backend asks the client (in the `hello` reply) to stream audio instead and
  feeds it to a `SpeechToTextProvider` plugin (faster-whisper locally, or a cloud API).
- Both paths produce the same `NormalisedSegment`s in session time; nothing downstream (commands,
  observer, editor, vault) knows which provider produced them.
- Adding a provider = one module implementing the interface + one config value; clients have
  their own `ClientTranscriber` interface for the same reason.
- Heavy provider dependencies are optional extras, not part of the default install.

## Consequences
- Client-side Google STT sends audio to Google; accepted by the student. Server mode exists for
  when privacy, quality or availability requires it.
- Timestamps from client recognizers are approximate; capture <-> speech alignment windows are
  configurable.
