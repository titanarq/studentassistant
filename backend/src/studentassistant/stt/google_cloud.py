"""Server-side STT in the cloud with Google Cloud Speech-to-Text v1 streaming (ADR-0008).

`stt.mode = "server"`, `stt.provider = "google-cloud"`. The client library comes with the optional
`google-cloud` extra (`uv sync --extra google-cloud`) and is imported only when the first stream
opens, inside a worker thread, so importing this module (or `studentassistant.stt`) needs nothing.

Credentials come from this machine, never from the vault: `credentials_file` (a service-account
JSON key, e.g. under `~/.config/studentassistant/`) in `[stt.options.google-cloud]`, or else the
library's Application Default Credentials (`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth
application-default login`).

Streaming: every chunk `feed` receives is queued on a gRPC bidirectional stream that a worker
thread drives; the interim and final results it gets back are turned into partial and final
`NormalisedSegment`s in session time, and `feed` returns whatever arrived since the previous call
(it never waits for the network). Google closes a stream after ~5 minutes of audio, so a new one
is opened every `stream_limit_seconds` of audio, and after a gap in the audio longer than 2 s
(shorter gaps are filled with silence).

Errors never reach the session: a stream that fails is logged, `status` turns `reconnecting` with
a Spanish `detail`, the audio of the next `retry_seconds` is dropped (`dropped_seconds`) and then a
new stream opens. A setup failure (library missing, no credentials) turns `status`
`unavailable` and the provider drops all audio from then on.

Options (`[stt.options.google-cloud]`): `credentials_file`, `language_code` (default: from
`stt.language`, `es` -> `es-ES`), `model` (`latest_long`), `phrases` (vocabulary hints, a list),
`automatic_punctuation` (true), `stream_limit_seconds` (240), `retry_seconds` (5),
`finish_timeout_seconds` (10).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from studentassistant.config import DEFAULT_GOOGLE_SPEECH_MODEL, DEFAULT_STT_LANGUAGE
from studentassistant.stt.models import AudioChunk, NormalisedSegment
from studentassistant.stt.provider import ProviderStatus, SpeechToTextProvider

logger = logging.getLogger(__name__)

SAMPLE_WIDTH = 2
DEFAULT_STREAM_LIMIT_SECONDS = 240.0
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_FINISH_TIMEOUT_SECONDS = 10.0
# A gap in the stream up to this long is filled with silence; a longer one opens a new stream.
MAX_PADDED_GAP_SECONDS = 2.0
# `stt.language` -> BCP-47 code when the option `language_code` is not set.
LANGUAGE_CODES = {"es": "es-ES", "en": "en-US"}

NOT_INSTALLED_MESSAGE = (
    "google-cloud-speech is not installed: run `uv sync --extra google-cloud` in backend/ "
    "to use stt.provider = 'google-cloud'"
)


class GoogleCloudSetupError(RuntimeError):
    """The provider cannot work at all (library missing, bad credentials); no retry helps."""


@dataclass(frozen=True)
class StreamResult:
    """One result of a stream: `end` is seconds from the stream's first audio sample."""

    text: str
    is_final: bool
    end: float | None = None
    confidence: float | None = None


class SpeechStreamClient(Protocol):
    """One streaming recognition: blocking, always called in a worker thread.

    Consumes `audio` (PCM16 mono at `sample_rate`) until it ends and yields the results as they
    arrive; raises `GoogleCloudSetupError` when it cannot work at all, anything else when the
    stream failed.
    """

    def stream(self, audio: Iterator[bytes], *, sample_rate: int) -> Iterator[StreamResult]: ...


def language_code(language: str) -> str:
    """`es` -> `es-ES`; a code that already names a region is kept."""
    return LANGUAGE_CODES.get(language, language) if "-" not in language else language


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(getattr(value, "seconds", 0)) + float(getattr(value, "nanos", 0)) / 1e9


class GoogleSpeechClient:
    """`SpeechStreamClient` over `google.cloud.speech.SpeechClient.streaming_recognize`."""

    def __init__(
        self,
        *,
        language_code: str,
        model: str = DEFAULT_GOOGLE_SPEECH_MODEL,
        credentials_file: str | None = None,
        phrases: list[str] | None = None,
        automatic_punctuation: bool = True,
    ) -> None:
        self.language_code = language_code
        self.model = model
        self.credentials_file = credentials_file
        self.phrases = list(phrases or [])
        # The session's vocabulary hints (#54), after the configured `phrases`; read per stream.
        self.session_phrases: list[str] = []
        self.automatic_punctuation = automatic_punctuation
        self._speech: Any = None
        self._client: Any = None
        self._lock = threading.Lock()

    def _connect(self) -> tuple[Any, Any]:
        with self._lock:
            if self._client is None:
                try:
                    speech = importlib.import_module("google.cloud.speech")
                except ImportError as error:
                    raise GoogleCloudSetupError(NOT_INSTALLED_MESSAGE) from error
                try:
                    if self.credentials_file:
                        path = Path(self.credentials_file).expanduser()
                        client = speech.SpeechClient.from_service_account_file(str(path))
                    else:
                        client = speech.SpeechClient()
                except Exception as error:  # missing file, bad key, no default credentials
                    raise GoogleCloudSetupError(
                        f"Google Cloud credentials unusable: {type(error).__name__}: {error}"
                    ) from error
                self._speech, self._client = speech, client
            return self._speech, self._client

    def set_vocabulary(self, hints: Sequence[str]) -> None:
        """The session's hints, used as phrase hints from the next stream on."""
        self.session_phrases = list(hints)

    def stream(self, audio: Iterator[bytes], *, sample_rate: int) -> Iterator[StreamResult]:
        speech, client = self._connect()
        phrases = list(dict.fromkeys([*self.phrases, *self.session_phrases]))
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            language_code=self.language_code,
            model=self.model,
            enable_automatic_punctuation=self.automatic_punctuation,
            speech_contexts=[speech.SpeechContext(phrases=phrases)] if phrases else [],
        )
        streaming = speech.StreamingRecognitionConfig(config=config, interim_results=True)
        requests = (speech.StreamingRecognizeRequest(audio_content=data) for data in audio)
        for response in client.streaming_recognize(streaming, requests):
            error = getattr(response, "error", None)
            if error is not None and getattr(error, "code", 0):
                raise RuntimeError(f"Speech-to-Text error {error.code}: {error.message}")
            # Interim results come as a stable part plus a less stable one: one partial.
            interim: list[StreamResult] = []
            for result in response.results:
                if not result.alternatives:
                    continue
                best = result.alternatives[0]
                item = StreamResult(
                    text=best.transcript.strip(),
                    is_final=bool(result.is_final),
                    end=_seconds(result.result_end_time),
                    confidence=(float(best.confidence) or None) if result.is_final else None,
                )
                if item.is_final:
                    yield item
                else:
                    interim.append(item)
            if interim:
                ends = [r.end for r in interim if r.end is not None]
                yield StreamResult(
                    text=" ".join(r.text for r in interim if r.text),
                    is_final=False,
                    end=max(ends) if ends else None,
                )


class _Stream:
    """One gRPC stream: its audio queue, worker thread and where it starts in session time."""

    def __init__(self, origin: float, sample_rate: int) -> None:
        self.origin = origin
        self.sample_rate = sample_rate
        self.sent_seconds = 0.0  # audio queued so far (the stream's clock)
        self.consumed_seconds = 0.0  # audio the client has taken from the queue
        self.last_final_end = 0.0  # stream offset where the next segment starts
        self.failed = False
        self.audio: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self.thread: threading.Thread | None = None

    def send(self, data: bytes) -> None:
        self.audio.put(data)
        self.sent_seconds += len(data) / (self.sample_rate * SAMPLE_WIDTH)

    def close(self) -> None:
        self.audio.put(None)

    def requests(self) -> Iterator[bytes]:
        while True:
            data = self.audio.get()
            if data is None:
                return
            self.consumed_seconds += len(data) / (self.sample_rate * SAMPLE_WIDTH)
            yield data


def _float_option(options: Mapping[str, Any], key: str, default: float) -> float:
    value = options.get(key)
    return default if value is None else float(value)


class GoogleCloudSpeechProvider(SpeechToTextProvider):
    """Google Cloud Speech-to-Text streaming: interim partials and finals, never raising on
    network or API errors (see `status`).

    `client=` replaces the Google client (tests); `clock=` the monotonic clock of the retry delay.
    """

    name: ClassVar[str] = "google-cloud"

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        language: str = DEFAULT_STT_LANGUAGE,
        client: SpeechStreamClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(options, language=language)
        opts = self.options
        self.stream_limit = _float_option(
            opts, "stream_limit_seconds", DEFAULT_STREAM_LIMIT_SECONDS
        )
        self.retry_seconds = _float_option(opts, "retry_seconds", DEFAULT_RETRY_SECONDS)
        self.finish_timeout = _float_option(
            opts, "finish_timeout_seconds", DEFAULT_FINISH_TIMEOUT_SECONDS
        )
        if self.stream_limit <= 0 or self.retry_seconds < 0 or self.finish_timeout <= 0:
            raise ValueError("google-cloud timing options must be positive")
        if client is None:
            phrases = opts.get("phrases") or []
            if isinstance(phrases, str):
                phrases = [phrases]
            credentials = opts.get("credentials_file")
            client = GoogleSpeechClient(
                language_code=str(opts.get("language_code") or language_code(language)),
                model=str(opts.get("model") or DEFAULT_GOOGLE_SPEECH_MODEL),
                credentials_file=str(credentials) if credentials else None,
                phrases=[str(p) for p in phrases],
                automatic_punctuation=bool(opts.get("automatic_punctuation", True)),
            )
        self.client = client
        self.clock = clock
        self.dropped_seconds = 0.0
        self._status = ProviderStatus("idle")
        self._stream: _Stream | None = None
        self._streams: list[_Stream] = []
        self._ready: list[NormalisedSegment] = []
        self._retry_at: float | None = None
        self._finished = False
        self._lock = threading.Lock()

    def set_vocabulary(self, hints: Sequence[str]) -> None:
        """Phrase hints for the next gRPC stream (a running one keeps its config): passed to the
        client when it takes them (`GoogleSpeechClient.set_vocabulary`)."""
        super().set_vocabulary(hints)
        setter = getattr(self.client, "set_vocabulary", None)
        if callable(setter):
            setter(list(hints))

    @property
    def status(self) -> ProviderStatus:
        with self._lock:
            return self._status

    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        if self._finished:
            raise RuntimeError("GoogleCloudSpeechProvider was fed after finish()")
        if chunk.sample_width != SAMPLE_WIDTH:
            raise ValueError(f"google-cloud needs 16-bit PCM, got {chunk.sample_width * 8}-bit")
        stream = self._stream_for(chunk)
        if stream is None:
            self.dropped_seconds += chunk.duration
            return self._take()
        end = stream.origin + stream.sent_seconds
        if chunk.start > end:
            gap = round((chunk.start - end) * chunk.sample_rate) * SAMPLE_WIDTH
            stream.send(bytes(gap))
        stream.send(chunk.data)
        return self._take()

    async def finish(self) -> list[NormalisedSegment]:
        if self._finished:
            return self._take()
        self._finished = True
        self._close_current()
        await asyncio.to_thread(self._join, self.finish_timeout)
        return self._take()

    # --- streams ------------------------------------------------------------------------------

    def _stream_for(self, chunk: AudioChunk) -> _Stream | None:
        """The stream `chunk` goes to (a new one when needed), or `None` to drop it."""
        with self._lock:
            if self._status.state == "unavailable":
                return None
            if self._retry_at is not None:
                if self.clock() < self._retry_at:
                    return None
                self._retry_at = None
        stream = self._stream
        if stream is not None:
            end = stream.origin + stream.sent_seconds
            if (
                stream.failed
                or stream.sample_rate != chunk.sample_rate
                or chunk.start - end > MAX_PADDED_GAP_SECONDS
                or stream.sent_seconds + chunk.duration > self.stream_limit
            ):
                self._close_current()
                stream = None
        if stream is None:
            stream = self._open(chunk.start, chunk.sample_rate)
        return stream

    def _open(self, origin: float, sample_rate: int) -> _Stream:
        stream = _Stream(origin, sample_rate)
        stream.thread = threading.Thread(
            target=self._run, args=(stream,), name="stt-google-cloud", daemon=True
        )
        self._stream = stream
        self._streams.append(stream)
        with self._lock:
            self._status = ProviderStatus("streaming", self._status.detail)
        stream.thread.start()
        return stream

    def _close_current(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _join(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for stream in self._streams:
            if stream.thread is not None:
                stream.thread.join(max(0.0, deadline - time.monotonic()))
                if stream.thread.is_alive():
                    logger.warning(
                        "google-cloud STT stream did not finish within %.1f s; its last "
                        "results are lost",
                        timeout,
                    )
        self._streams = [s for s in self._streams if s.thread is not None and s.thread.is_alive()]

    # --- worker-thread side -------------------------------------------------------------------

    def _run(self, stream: _Stream) -> None:
        try:
            for result in self.client.stream(stream.requests(), sample_rate=stream.sample_rate):
                self._accept(stream, result)
        except GoogleCloudSetupError as error:
            stream.failed = True
            logger.error("google-cloud STT unavailable: %s", error)
            with self._lock:
                self._status = ProviderStatus(
                    "unavailable",
                    "La transcripción con Google Cloud no está disponible "
                    "(revisa la instalación y las credenciales); el audio no se transcribe.",
                )
        except Exception as error:
            stream.failed = True
            logger.warning("google-cloud STT stream failed: %s: %s", type(error).__name__, error)
            with self._lock:
                if self._status.state != "unavailable":
                    self._retry_at = self.clock() + self.retry_seconds
                    self._status = ProviderStatus(
                        "reconnecting",
                        "Se ha perdido la conexión con Google Cloud; se reintenta en "
                        f"{self.retry_seconds:g} s y el audio de mientras no se transcribe.",
                    )

    def _accept(self, stream: _Stream, result: StreamResult) -> None:
        # Never past the audio the client has actually taken.
        consumed = stream.consumed_seconds
        offset = consumed if result.end is None else min(max(result.end, 0.0), consumed)
        start_offset = min(stream.last_final_end, offset)
        if result.is_final:
            stream.last_final_end = max(stream.last_final_end, offset)
        if not result.text:
            return
        confidence = result.confidence
        if confidence is not None:
            confidence = min(1.0, max(0.0, confidence))
        segment = NormalisedSegment(
            start=stream.origin + start_offset,
            end=stream.origin + offset,
            text=result.text,
            provider=self.name,
            confidence=confidence,
            is_final=result.is_final,
        )
        with self._lock:
            self._ready.append(segment)
            if self._status.state == "streaming" and self._status.detail is not None:
                self._status = ProviderStatus("streaming")

    def _take(self) -> list[NormalisedSegment]:
        with self._lock:
            ready, self._ready = self._ready, []
        return ready


__all__ = [
    "GoogleCloudSetupError",
    "GoogleCloudSpeechProvider",
    "GoogleSpeechClient",
    "ProviderStatus",
    "SpeechStreamClient",
    "StreamResult",
    "language_code",
]
