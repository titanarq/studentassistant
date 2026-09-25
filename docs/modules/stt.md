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
- Vocabulary hints (subject and topic names, observer concepts) assembled and capped here, applied
  by server-side providers (Whisper `hotwords`); the server sends the same list to the capture
  client (protocol 1.4 `vocabulary_hints`).

## Boundaries
- Never calls an LLM. Runs model inference off the event loop. No module outside `stt`
  imports a concrete provider.

## Public surface
Everything below is importable from `studentassistant.stt` (the fakes from
`studentassistant.stt.fakes`); importing the package loads no concrete provider.

- `NormalisedSegment` -- what both paths produce: `start`/`end` (session seconds, `end >= start`),
  `text`, `provider`, `confidence` (`None` or 0..1), `is_final` (default `True`).
- `ClientSegment` -- a client recognizer's segment on the client's clock: `client_start`,
  `client_end`, `text`, `confidence`, `is_final`. Its own input model, not the wire message.
- `AudioChunk` -- server-mode audio: `start` (session seconds), mono PCM `data`, `sample_rate`
  (16000), `sample_width` (2); `duration`, `end`.
- `SpeechToTextProvider` -- the server-side plugin interface (below).
- `TranscriptSink` -- `async ingest(ClientSegment) -> NormalisedSegment`, carrying the configured
  client provider name; `InMemoryTranscriptSink(provider, clock_offset=0.0)` /
  `InMemoryTranscriptSink.from_settings(settings.stt)` keeps them in `segments`.
- Registry: `get_provider(name, options=None, *, language="es")`,
  `provider_from_settings(settings.stt)` (server mode only, else `NotServerModeError`),
  `provider_class(name)`, `registered_providers()`, `register_provider(name, cls)`,
  `unregister_provider(name)`; an unknown name raises `UnknownProviderError` listing the
  registered ones. `ENTRY_POINT_GROUP = "studentassistant.stt_providers"`.
- `TranscriptAssembler` (`stt/transcript.py`, pure logic) -- one session's canonical transcript:
  `add(final) -> NormalisedSegment | None` returns what to append, `seed(segment)` records one
  already in `transcript.jsonl` (a resumed session). Rules, for finals in arrival order (partials
  and blank finals are never written; words compare case-insensitively without punctuation):
  1. a final with the same span and text as a written one is dropped;
  2. a final whose span lies inside a written final with the same text is dropped;
  3. a final that overlaps one of the last `OVERLAP_LOOKBACK` (8) written finals in time (the last
     one also when it starts before it) is trimmed of the words it repeats: all its words in a
     row inside that final -> dropped; otherwise the longest prefix repeating that final's end
     is cut and its start moves to that final's end (a restarted recognizer never writes the
     same words twice);
  4. written starts never decrease: a late final is placed after the last written one (start
     clamped to its start) and is never dropped when it carries new words.
- `TranscriptPipeline(bus, lookup, *, clock=...)` (`stt/pipeline.py`) -- subscribes to the bus for
  `transcript.final` and `session.ended` (`start()` / `await stop()`, the app's lifespan does both
  on `app.state.transcripts`), runs one assembler per session and appends each segment with
  `Session.append_transcript` (session ms) in a worker thread. `bus` is anything with the
  `EventBus` protocol's `subscribe` (the server's `SessionBus`); `lookup: SessionLookup` maps a
  session id to its open vault `Session` (the app passes `SessionBus.attached`), so `stt` never
  imports `server`. A final after `session.ended`, or refused by the vault (`SessionEndedError`),
  is logged and dropped; a `SecretRefused` one is logged and skipped; nothing is raised into the
  bus. `await drain()` returns once every event delivered so far is processed.
  Latency log: logger `studentassistant.stt.pipeline`, one INFO record per appended segment,
  `latency_ms` = session time of the append minus the segment's end, with `session_id`,
  `provider` and `transcript_seq` as record attributes.
  `SessionService.end` awaits `drain()` (a before-close end hook the app adds) after publishing
  `session.ended` and before the vault ends the session, so every final published before the end
  is written.
- Vocabulary hints (`stt/vocabulary.py`, pure logic, #54):
  `vocabulary_hints(*, subject, topic, concepts, max_terms, max_chars) -> list[str]` -- the
  subject's name, the topic's name, then `concepts` (given oldest first) newest first; whitespace
  collapsed; blank terms, terms over 100 characters and repeats (case and accents ignored)
  skipped; at most `max_terms` (never more than the protocol's 50) and, joined with `", "`, at
  most `max_chars` characters (a term that does not fit is skipped, a shorter later one may still
  be taken); `max_terms <= 0` gives `[]`. `vocabulary_hints_from_settings(settings.stt, ...)`
  takes the caps from `[stt]`; `hotwords_text(hints)` joins them (`None` when empty). `stt` only
  assembles: the server gathers the terms (vault + observer) and never lets `stt` import them.
- `SpeechToTextProvider.set_vocabulary(hints)` -- replace the session's hints at any time (before
  the first chunk or between chunks); the latest list is in `vocabulary` (a tuple, `()` at first).
  The default only stores them; a provider that can bias recognition applies them from its next
  inference on. `BufferedProvider` passes them straight to the wrapped provider. The gateway calls
  it at every handshake and whenever the observer adds a concept.
- `BufferedProvider(inner, *, max_backlog_seconds=...)` (`stt/buffered.py`) -- a
  `SpeechToTextProvider` wrapping another one so the gateway never waits for inference: `feed`
  enqueues the chunk and returns what `inner` produced since the previous call; one background
  task feeds `inner` in order. While more than `max_backlog_seconds` of audio is queued, buffered
  partials a newer segment supersedes are dropped (`dropped` counts them); finals never are.
  `finish()` drains the queue, flushes `inner` and returns the rest; `close()` stops the task.
  `buffered_provider_from_settings(settings.stt)` wraps `provider_from_settings`; the app's
  gateway builds its server-mode providers with it.
- `FasterWhisperProvider` (`stt/faster_whisper.py`, entry point `faster-whisper`; needs the
  `whisper` extra, `uv sync --extra whisper`, imported only when the model is first used) -- local
  faster-whisper. Every `partial_interval_seconds` of fed audio it runs Silero VAD over the audio
  not yet committed, in a worker thread: each speech span followed by `min_silence_ms` of silence
  is transcribed and yielded as finals (and its audio dropped), the speech still going on as one
  partial covering it; an utterance past `max_utterance_seconds` is committed anyway; `finish`
  commits the rest. A gap in the stream up to 2 s is filled with silence, a longer one commits what
  came before. Only 16 kHz PCM16 is accepted. The model loads lazily (first chunk, in the worker
  thread); `device = "auto"` picks CUDA (`int8_float16`) when CTranslate2 sees a device and CPU
  (`int8`) otherwise, and falls back to the CPU once when the first CUDA transcription fails (a
  missing cuBLAS/cuDNN). Before choosing a device it calls `cuda.preload()`. The session's
  vocabulary hints go to Whisper as `hotwords` (after the configured `hotwords`, if any; the
  configured `initial_prompt` is passed unchanged); `hotwords` shows what the next transcription
  gets.
- `stt/cuda.py`: the `whisper` extra also pulls the `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`
  (cuDNN 9) wheels on Linux; `preload()` loads `libcublasLt.so.12`, `libcublas.so.12` and
  `libcudnn.so.9` from their site-packages `lib/` directories with `RTLD_GLOBAL` (once, never
  raises, a no-op without the wheels), so CTranslate2's later lookup by name finds them without
  `LD_LIBRARY_PATH`. `check() -> str | None` opens both by name and creates/destroys a cuBLAS and
  a cuDNN handle; `None` when they work, else a Spanish reason (used by `doctor`).
- `backend=` takes any `WhisperBackend` (`speech_spans`, `transcribe(audio, *, hotwords=None)`;
  blocking) for tests. `studentassistant stt download` fetches the configured model (as `setup`
  does).
- `GoogleCloudSpeechProvider` (`stt/google_cloud.py`, entry point `google-cloud`; needs the
  `google-cloud` extra, `uv sync --extra google-cloud`, imported only when the first stream
  opens) -- Google Cloud Speech-to-Text v1 streaming with interim results. The session's
  vocabulary hints (`set_vocabulary`) join the configured `phrases` as phrase hints
  (`SpeechContext`) from the next gRPC stream on (a running stream keeps its config; streams
  rotate at `stream_limit_seconds`); a custom client without `set_vocabulary` ignores them. `feed` queues the chunk
  on a gRPC stream a worker thread drives and returns the partials/finals that arrived since the
  previous call (never waits for the network); a segment starts where the stream's previous final
  ended and ends at the result's `result_end_time`, clamped to the audio the stream consumed. A
  new stream opens every `stream_limit_seconds` of audio (Google caps a stream at ~5 min), on a
  gap over 2 s (shorter gaps are filled with silence) and on a new sample rate; only 16-bit PCM.
  Credentials come from this machine (`credentials_file` or Application Default Credentials),
  never the vault. Errors never raise into the session: `status` (`ProviderStatus(state,
  detail)`, `state` in `idle | streaming | reconnecting | unavailable`, `detail` a Spanish
  sentence) turns `reconnecting` when a stream fails (the audio of the next `retry_seconds` is
  dropped and counted in `dropped_seconds`, then a new stream opens) and `unavailable` when the
  library or the credentials are missing (all audio dropped from then on). `finish` half-closes
  the stream and waits at most `finish_timeout_seconds` for its last results. `client=` takes any
  `SpeechStreamClient` (`stream(audio, *, sample_rate) -> Iterator[StreamResult]`, blocking) for
  tests; `GoogleSpeechClient` is the real one.
- Fakes: `FakeProvider` (registered as `fake`; `segments=` or `options["segments"]`, each
  scripted segment is yielded once the fed audio reaches its `end`, the rest on `finish`) and
  `ScriptedClientSource(segments)` (`play_into(sink)`).

## Configuration
```toml
[stt]
mode = "client"          # client | server
provider = "web-speech"  # web-speech | android-speech (client); faster-whisper, a cloud id, fake (server)
language = "es"
max_backlog_seconds = 10.0  # server mode: queued audio past which superseded partials drop
vocabulary_max_terms = 30   # vocabulary hints: most terms (0 = off; at most 50)
vocabulary_max_chars = 500  # vocabulary hints: most characters, joined with ", "

[stt.options.faster-whisper]  # free-form table per provider name, passed to its constructor
model = "large-v3-turbo"        # the default
device = "auto"                 # auto | cuda | cpu
# download_root = "~/models"    # unset: the Hugging Face cache
# compute_type = "int8_float16" # default: int8_float16 on CUDA, int8 on CPU
# beam_size = 5
# initial_prompt = "Clase de cálculo."     # a fixed prompt
# hotwords = "derivadas, integrales"        # fixed hint phrases, before the session's hints
# partial_interval_seconds = 1.0
# min_silence_ms = 600          # silence that ends an utterance
# max_utterance_seconds = 20.0
# vad_threshold = 0.5
```
```toml
[stt.options.google-cloud]  # stt.mode = "server", stt.provider = "google-cloud"
# credentials_file = "~/.config/studentassistant/google-stt.json"  # unset: Application Default Credentials
# language_code = "es-ES"       # default: from stt.language (es -> es-ES)
model = "latest_long"           # the default
# phrases = ["derivada", "integral"]  # fixed phrase hints, before the session's hints
# automatic_punctuation = true
# stream_limit_seconds = 240.0  # audio per stream before a new one opens
# retry_seconds = 5.0           # after a failed stream, audio dropped before reconnecting
# finish_timeout_seconds = 10.0
```
Env overrides: `SA_STT__MODE`, `SA_STT__PROVIDER`, `SA_STT__LANGUAGE`,
`SA_STT__MAX_BACKLOG_SECONDS`, `SA_STT__VOCABULARY_MAX_TERMS`, `SA_STT__VOCABULARY_MAX_CHARS`.

## How to add a server-side provider
1. One module, e.g. `studentassistant/stt/faster_whisper.py`, with a subclass of
   `SpeechToTextProvider`: set `name`, accept `(options, *, language)` in `__init__` (call
   `super().__init__`), implement `async feed(chunk: AudioChunk) -> list[NormalisedSegment]`
   (partials or finals the chunk made recognisable, in session time, `provider=self.name`) and
   `async finish() -> list[NormalisedSegment]` (flush). Run inference off the event loop
   (`asyncio.to_thread` or a process); import heavy dependencies inside the module only.
2. One entry point in `backend/pyproject.toml` (heavy dependencies go in an optional extra):
   ```toml
   [project.entry-points."studentassistant.stt_providers"]
   faster-whisper = "studentassistant.stt.faster_whisper:FasterWhisperProvider"
   ```
   Re-sync the environment (`uv sync`) so the entry point is installed. An out-of-tree package
   can declare the same group.
3. One config value: `stt.mode = "server"`, `stt.provider = "faster-whisper"`, options under
   `[stt.options.faster-whisper]`. Nothing outside `stt` names the class.

A client-side recognizer needs no backend code: its name is just `stt.provider` in client mode,
stamped on every segment by the `TranscriptSink`.

## Tests
Pipeline and grammar tested with `FakeProvider` and scripted segments; hint assembly in
`test_stt_vocabulary.py`, the gateway's hints (hello.ack, notice, provider) in
`tests/server/test_ws_vocabulary_hints.py`; `FasterWhisperProvider`
with a scripted `WhisperBackend` and a stand-in `faster_whisper` module. `GoogleCloudSpeechProvider`
with a scripted `SpeechStreamClient` and a stand-in `google.cloud.speech` module. Real-model tests are
`integration`: `SA_TEST_SPANISH_WAV=<16 kHz mono PCM16 WAV> [SA_TEST_SPANISH_WORDS="..."]
uv run pytest -m integration -k spanish` (needs the `whisper` extra; the Google one also
`SA_TEST_GOOGLE_CREDENTIALS=<key.json>` or `SA_TEST_GOOGLE_CLOUD=1` with Application Default
Credentials, and the `google-cloud` extra).
