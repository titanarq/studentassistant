# Module: stt

**Lives in:** `backend/src/studentassistant/stt/`.

## Responsibility
Decision: ADR-0008.
- Provider layer: `stt.mode = client|server`, `stt.provider`; a `TranscriptSink` ingesting
  client-side segments (Web Speech API, Android SpeechRecognizer) and a `SpeechToTextProvider`
  plugin interface for server-side providers fed with audio (faster-whisper, cloud); registry;
  `FakeProvider` for tests.
- Pipeline: client times -> session time, dedupe/merge, partial and final `NormalisedSegment`s;
  finals appended to the transcript via `vault` and published on the bus.
- Voice-command grammar (ADR-0006): YAML phrases -> command events, debounced.
- Vocabulary hints (topic names, concepts) passed as initial prompt / hotwords.

## Boundaries
- Never calls an LLM. Runs model inference off the event loop. No module outside `stt`
  imports a concrete provider.

## Tests
Pipeline and grammar tested with `FakeProvider` and scripted segments; real-model tests are
`integration`.
