"""Server-side STT on this PC's GPU with faster-whisper (ADR-0008): `stt.provider = faster-whisper`.

faster-whisper comes with the optional `whisper` extra (`uv sync --extra whisper`); it (and
`ctranslate2`) is imported only when the model is first needed, inside a worker thread, so importing
this module (or `studentassistant.stt`) never pulls CUDA libraries in.

Streaming over a non-streaming model: the provider keeps the audio not yet committed as finals and,
every `partial_interval_seconds` of new audio, runs Silero VAD (bundled with faster-whisper) over
it in a worker thread:

- every speech span followed by at least `min_silence_ms` of silence is complete: that stretch is
  transcribed and its segments come out as finals, and the audio up to its end is dropped;
- the speech still going on is transcribed as one partial (`is_final=False`) covering it; an
  utterance longer than `max_utterance_seconds` is committed as finals anyway;
- `finish` commits whatever speech is left.

Options (`[stt.options.faster-whisper]`): `model` (default `large-v3-turbo`), `device` (`auto`:
CUDA when CTranslate2 sees a device, CPU otherwise; `cuda`; `cpu`), `download_root` (unset: the
Hugging Face cache), `compute_type` (default `int8_float16` on CUDA, `int8` on CPU), `beam_size`
(5), `initial_prompt` (vocabulary hints), `partial_interval_seconds` (1.0), `min_silence_ms`
(600), `max_utterance_seconds` (20.0), `vad_threshold` (0.5).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

import numpy as np

from studentassistant.config import (
    DEFAULT_STT_LANGUAGE,
    DEFAULT_WHISPER_DEVICE,
    DEFAULT_WHISPER_MODEL,
)
from studentassistant.stt import cuda
from studentassistant.stt.models import AudioChunk, NormalisedSegment
from studentassistant.stt.provider import SpeechToTextProvider

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CUDA_COMPUTE_TYPE = "int8_float16"
CPU_COMPUTE_TYPE = "int8"
DEFAULT_BEAM_SIZE = 5
DEFAULT_PARTIAL_INTERVAL_SECONDS = 1.0
DEFAULT_MIN_SILENCE_MS = 600
DEFAULT_MAX_UTTERANCE_SECONDS = 20.0
DEFAULT_VAD_THRESHOLD = 0.5
# Silence kept before the first speech so a word starting at the buffer's edge is not cut.
LEADING_PAD_SECONDS = 0.5
# A gap in the stream up to this long is filled with silence; a longer one commits what came before.
MAX_PADDED_GAP_SECONDS = 2.0
# Whisper's own "this is not speech" test: a segment above this no-speech probability whose average
# log-probability is also below the threshold is dropped (a hallucination on noise).
NO_SPEECH_THRESHOLD = 0.6
LOGPROB_THRESHOLD = -1.0

NOT_INSTALLED_MESSAGE = (
    "faster-whisper is not installed: run `uv sync --extra whisper` in backend/ "
    "to use stt.provider = 'faster-whisper'"
)


class WhisperNotInstalledError(RuntimeError):
    """The `whisper` extra is not installed."""


@dataclass(frozen=True)
class Recognised:
    """A stretch of text the model recognised, in seconds from the start of the audio it got."""

    start: float
    end: float
    text: str
    confidence: float | None = None


class WhisperBackend(Protocol):
    """What the provider needs from the model; blocking calls, always run in a worker thread."""

    def speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]]:
        """Speech in `audio` (mono float32 at 16 kHz) as `(start, end)` sample indices."""
        ...

    def transcribe(self, audio: np.ndarray) -> list[Recognised]:
        """Transcribe `audio`, which is all speech (VAD already applied)."""
        ...


def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise WhisperNotInstalledError(NOT_INSTALLED_MESSAGE) from error


def resolve_device(device: str, compute_type: str | None = None) -> tuple[str, str]:
    """`(device, compute_type)` for CTranslate2: `auto` becomes `cuda` when a device is visible."""
    if device == "auto":
        try:
            count = int(_import("ctranslate2").get_cuda_device_count())
        except WhisperNotInstalledError:
            raise
        except Exception:
            count = 0
        device = "cuda" if count > 0 else "cpu"
    if compute_type is None:
        compute_type = CUDA_COMPUTE_TYPE if device == "cuda" else CPU_COMPUTE_TYPE
    return device, compute_type


def _confidence(avg_logprob: float | None) -> float | None:
    if avg_logprob is None:
        return None
    return min(1.0, max(0.0, math.exp(avg_logprob)))


class FasterWhisperBackend:
    """`WhisperBackend` over `faster_whisper.WhisperModel` and its Silero VAD; loads lazily."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_WHISPER_MODEL,
        device: str = DEFAULT_WHISPER_DEVICE,
        download_root: str | None = None,
        compute_type: str | None = None,
        language: str = DEFAULT_STT_LANGUAGE,
        beam_size: int = DEFAULT_BEAM_SIZE,
        initial_prompt: str | None = None,
        min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
        vad_threshold: float = DEFAULT_VAD_THRESHOLD,
    ) -> None:
        self.model_name = model
        self.device = device
        self.download_root = download_root
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self.min_silence_ms = min_silence_ms
        self.vad_threshold = vad_threshold
        self._model: Any = None
        self._vad: Any = None
        self._lock = threading.Lock()
        # `auto` resolved to CUDA but no transcription has worked yet: a missing CUDA library
        # (cuBLAS, cuDNN) only shows on the first encode, and then the model is reloaded on CPU.
        self._cuda_unproven = False
        self._explicit_compute_type = compute_type is not None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the model (blocking; downloads it when it is not cached). Idempotent."""
        with self._lock:
            if self._model is not None:
                return
            module = _import("faster_whisper")
            self._vad = _import("faster_whisper.vad")
            if self.device != "cpu":
                cuda.preload()  # the `whisper` extra's cuBLAS/cuDNN wheels, before any encode
            device, compute_type = resolve_device(self.device, self.compute_type)
            logger.info(
                "loading faster-whisper model %s on %s (%s)", self.model_name, device, compute_type
            )
            self._model = module.WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
                download_root=self.download_root,
            )
            self._cuda_unproven = self.device == "auto" and device == "cuda"
            self.device, self.compute_type = device, compute_type

    def _fall_back_to_cpu(self, error: Exception) -> None:
        logger.warning(
            "faster-whisper failed on CUDA (%s); falling back to the CPU (device = auto)", error
        )
        module = _import("faster_whisper")
        compute_type = self.compute_type if self._explicit_compute_type else CPU_COMPUTE_TYPE
        with self._lock:
            self._model = module.WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=compute_type,
                download_root=self.download_root,
            )
            self.device, self.compute_type = "cpu", compute_type
            self._cuda_unproven = False

    def speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]]:
        self.load()
        options = self._vad.VadOptions(
            threshold=self.vad_threshold, min_silence_duration_ms=self.min_silence_ms
        )
        spans = self._vad.get_speech_timestamps(audio, options, SAMPLE_RATE)
        return [(int(s["start"]), int(s["end"])) for s in spans]

    def transcribe(self, audio: np.ndarray) -> list[Recognised]:
        self.load()
        if not self._cuda_unproven:
            return self._transcribe(audio)
        try:
            recognised = self._transcribe(audio)
        except RuntimeError as error:
            self._fall_back_to_cpu(error)
            return self._transcribe(audio)
        self._cuda_unproven = False
        return recognised

    def _transcribe(self, audio: np.ndarray) -> list[Recognised]:
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            initial_prompt=self.initial_prompt,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        out: list[Recognised] = []
        for segment in segments:  # a generator: decoding happens here
            text = segment.text.strip()
            avg_logprob = getattr(segment, "avg_logprob", None)
            no_speech = getattr(segment, "no_speech_prob", 0.0) or 0.0
            if not text:
                continue
            if (
                no_speech > NO_SPEECH_THRESHOLD
                and avg_logprob is not None
                and avg_logprob < LOGPROB_THRESHOLD
            ):
                continue
            out.append(
                Recognised(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=text,
                    confidence=_confidence(avg_logprob),
                )
            )
        return out


def _float_option(options: Mapping[str, Any], key: str, default: float) -> float:
    value = options.get(key)
    return default if value is None else float(value)


class FasterWhisperProvider(SpeechToTextProvider):
    """Local faster-whisper: VAD-segmented finals, a partial for the ongoing speech every ~1 s.

    `backend=` replaces the model (tests); by default a `FasterWhisperBackend` built from the
    options, loaded on first use in a worker thread. Calls must not overlap (`BufferedProvider`,
    which the gateway wraps every provider in, feeds one chunk at a time).
    """

    name: ClassVar[str] = "faster-whisper"

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        language: str = DEFAULT_STT_LANGUAGE,
        backend: WhisperBackend | None = None,
    ) -> None:
        super().__init__(options, language=language)
        opts = self.options
        self.partial_interval = _float_option(
            opts, "partial_interval_seconds", DEFAULT_PARTIAL_INTERVAL_SECONDS
        )
        self.min_silence = _float_option(opts, "min_silence_ms", DEFAULT_MIN_SILENCE_MS) / 1000
        self.max_utterance = _float_option(
            opts, "max_utterance_seconds", DEFAULT_MAX_UTTERANCE_SECONDS
        )
        if self.partial_interval <= 0 or self.max_utterance <= 0 or self.min_silence < 0:
            raise ValueError("faster-whisper timing options must be positive")
        if backend is None:
            root = opts.get("download_root")
            backend = FasterWhisperBackend(
                model=str(opts.get("model") or DEFAULT_WHISPER_MODEL),
                device=str(opts.get("device") or DEFAULT_WHISPER_DEVICE),
                download_root=str(Path(root).expanduser()) if root else None,
                compute_type=opts.get("compute_type") or None,
                language=language,
                beam_size=int(opts.get("beam_size") or DEFAULT_BEAM_SIZE),
                initial_prompt=opts.get("initial_prompt") or None,
                min_silence_ms=round(self.min_silence * 1000),
                vad_threshold=_float_option(opts, "vad_threshold", DEFAULT_VAD_THRESHOLD),
            )
        self.backend = backend
        # Audio not yet committed as finals, and the session time of its first sample.
        self._audio = np.zeros(0, dtype=np.float32)
        self._origin = 0.0
        self._since_step = 0.0
        self._finished = False
        self._lock = asyncio.Lock()

    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        if self._finished:
            raise RuntimeError("FasterWhisperProvider was fed after finish()")
        if chunk.sample_rate != SAMPLE_RATE or chunk.sample_width != SAMPLE_WIDTH:
            raise ValueError(
                f"faster-whisper needs 16-bit PCM at {SAMPLE_RATE} Hz, got "
                f"{chunk.sample_width * 8}-bit at {chunk.sample_rate} Hz"
            )
        async with self._lock:
            out: list[NormalisedSegment] = []
            samples = np.frombuffer(chunk.data, dtype="<i2").astype(np.float32) / 32768.0
            buffered_end = self._origin + len(self._audio) / SAMPLE_RATE
            if not len(self._audio):
                self._origin = chunk.start
            elif chunk.start - buffered_end > MAX_PADDED_GAP_SECONDS:
                out.extend(await asyncio.to_thread(self._step, True))
                self._audio = np.zeros(0, dtype=np.float32)
                self._origin = chunk.start
            elif chunk.start > buffered_end:
                gap = round((chunk.start - buffered_end) * SAMPLE_RATE)
                self._audio = np.concatenate([self._audio, np.zeros(gap, dtype=np.float32)])
            self._audio = np.concatenate([self._audio, samples])
            self._since_step += chunk.duration
            if self._since_step >= self.partial_interval:
                self._since_step = 0.0
                out.extend(await asyncio.to_thread(self._step, False))
            return out

    async def finish(self) -> list[NormalisedSegment]:
        async with self._lock:
            if self._finished:
                return []
            self._finished = True
            if not len(self._audio):
                return []
            return await asyncio.to_thread(self._step, True)

    # --- worker-thread side -------------------------------------------------------------------

    def _segment(
        self, offset: int, rec: Recognised, limit: int, *, final: bool
    ) -> NormalisedSegment:
        base = self._origin + offset / SAMPLE_RATE
        cap = self._origin + limit / SAMPLE_RATE
        start = min(max(base, base + rec.start), cap)
        end = min(max(start, base + rec.end), cap)
        return NormalisedSegment(
            start=start,
            end=end,
            text=rec.text,
            provider=self.name,
            confidence=rec.confidence,
            is_final=final,
        )

    def _step(self, commit_all: bool) -> list[NormalisedSegment]:
        """VAD over the buffered audio; commit complete speech as finals, the rest as a partial."""
        audio = self._audio
        total = len(audio)
        spans = [(s, min(e, total)) for s, e in self.backend.speech_spans(audio) if e > s]
        if not spans:
            self._drop(max(0, total - round(LEADING_PAD_SECONDS * SAMPLE_RATE)))
            return []
        silence = round(self.min_silence * SAMPLE_RATE)
        complete = [span for span in spans if commit_all or span[1] + silence <= total]
        ongoing = spans[len(complete) :]
        if ongoing and (total - ongoing[0][0]) / SAMPLE_RATE >= self.max_utterance:
            complete, ongoing = spans, []
        out: list[NormalisedSegment] = []
        if not complete:
            # Only speech still going on: drop the silence before it (keeping the leading pad).
            self._drop(max(0, ongoing[0][0] - round(LEADING_PAD_SECONDS * SAMPLE_RATE)))
        else:
            first, cut = complete[0][0], complete[-1][1]
            for rec in self.backend.transcribe(audio[first:cut]):
                out.append(self._segment(first, rec, cut, final=True))
            if ongoing:
                self._drop(cut)
            else:
                # Keep the silence after the last speech, trimmed to the leading pad.
                self._drop(max(cut, total - round(LEADING_PAD_SECONDS * SAMPLE_RATE)))
        if ongoing:
            start = ongoing[0][0] - (total - len(self._audio))
            start = max(0, start)
            recognised = self.backend.transcribe(self._audio[start:])
            text = " ".join(r.text for r in recognised).strip()
            if text:
                confidences = [r.confidence for r in recognised if r.confidence is not None]
                partial = Recognised(
                    start=0.0,
                    end=(len(self._audio) - start) / SAMPLE_RATE,
                    text=text,
                    confidence=min(confidences) if confidences else None,
                )
                out.append(self._segment(start, partial, len(self._audio), final=False))
        return out

    def _drop(self, samples: int) -> None:
        if samples <= 0:
            return
        self._audio = self._audio[samples:]
        self._origin += samples / SAMPLE_RATE


__all__ = [
    "FasterWhisperBackend",
    "FasterWhisperProvider",
    "Recognised",
    "WhisperBackend",
    "WhisperNotInstalledError",
    "resolve_device",
]
