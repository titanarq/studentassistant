# Module: stt

**Lives in:** `backend/src/studentassistant/stt/`.

## Responsibility
- `Transcriber` interface; faster-whisper implementation (config: model, device, compute type;
  Spanish; word timestamps); `FakeTranscriber` for tests.
- Streaming pipeline: VAD segmentation, partial hypotheses (~1 s cadence) and final segments
  with session-relative timestamps; final segments appended to the transcript via `vault` and
  published on the bus.
- Voice-command grammar (ADR-0006): YAML phrases -> command events, debounced.
- Vocabulary hints (topic names, concepts) passed as initial prompt / hotwords.

## Boundaries
- Never calls an LLM. Runs model inference off the event loop.

## Tests
Pipeline and grammar tested with `FakeTranscriber` and scripted text; real-model tests are
`integration`.
