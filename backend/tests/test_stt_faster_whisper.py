"""`FasterWhisperProvider`: VAD-segmented finals, ~1 s partials, off the event loop, lazy model.

No test here loads a real model: a scripted `WhisperBackend` reads "words" from the audio
(a run of samples at amplitude level k is word k, zero is silence), and the backend adapter is
tested over a stand-in `faster_whisper` module. The real model is `integration` only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.config import SttSettings
from studentassistant.stt import (
    AudioChunk,
    BufferedProvider,
    NormalisedSegment,
    get_provider,
    provider_from_settings,
)
from studentassistant.stt.faster_whisper import (
    FasterWhisperBackend,
    FasterWhisperProvider,
    Recognised,
    WhisperNotInstalledError,
    resolve_device,
)
from whisper_fakes import hide_faster_whisper, install_fakes

pytestmark = pytest.mark.anyio

RATE = 16_000
WORDS = {1: "hola", 2: "clase", 3: "derivadas", 4: "integral"}
LEVEL = 327  # int16 step per word level (~0.01 in float)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pcm(*parts: tuple[int, float]) -> bytes:
    """PCM16 of `(word level or 0 for silence, seconds)` parts."""
    return b"".join(
        np.full(round(seconds * RATE), level * LEVEL, dtype="<i2").tobytes()
        for level, seconds in parts
    )


def chunks(data: bytes, start: float = 0.0, seconds: float = 0.25) -> list[AudioChunk]:
    step = round(seconds * RATE) * 2
    return [
        AudioChunk(start=start + i / 2 / RATE, data=data[i : i + step])
        for i in range(0, len(data), step)
    ]


def _runs(audio: np.ndarray) -> list[tuple[int, int, int]]:
    """`(start, end, level)` runs of equal non-zero level."""
    levels = np.rint(audio * 32768 / LEVEL).astype(int)
    runs: list[tuple[int, int, int]] = []
    start = None
    for i, level in enumerate([*levels.tolist(), 0]):
        if start is not None and level != levels[start]:
            runs.append((start, i, int(levels[start])))
            start = None
        if start is None and level != 0 and i < len(levels):
            start = i
    return runs


class ScriptedBackend:
    """Speech = non-zero samples (runs closer than `merge` seconds are one span)."""

    def __init__(self, merge: float = 0.3) -> None:
        self.merge = round(merge * RATE)
        self.vad_calls = 0
        self.transcribed: list[float] = []  # seconds of audio per transcribe call
        self.threads: set[int] = set()

    def speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]]:
        self.vad_calls += 1
        self.threads.add(threading.get_ident())
        spans: list[tuple[int, int]] = []
        for start, end, _ in _runs(audio):
            if spans and start - spans[-1][1] < self.merge:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
        return spans

    def transcribe(self, audio: np.ndarray) -> list[Recognised]:
        self.threads.add(threading.get_ident())
        self.transcribed.append(len(audio) / RATE)
        return [
            Recognised(start=s / RATE, end=e / RATE, text=WORDS[level], confidence=0.9)
            for s, e, level in _runs(audio)
        ]


def provider(backend: ScriptedBackend, **options: Any) -> FasterWhisperProvider:
    return FasterWhisperProvider(options, backend=backend)


async def feed_all(p: FasterWhisperProvider, pieces: list[AudioChunk]) -> list[NormalisedSegment]:
    out: list[NormalisedSegment] = []
    for piece in pieces:
        out.extend(await p.feed(piece))
    return out


async def test_speech_followed_by_silence_comes_out_as_finals_in_session_time() -> None:
    backend = ScriptedBackend()
    p = provider(backend)
    data = pcm((0, 0.5), (1, 0.6), (0, 0.1), (2, 0.8), (0, 1.5))

    out = await feed_all(p, chunks(data, start=10.0))
    finals = [s for s in out if s.is_final]

    assert [s.text for s in finals] == ["hola", "clase"]
    assert finals[0].start == pytest.approx(10.5)
    assert finals[0].end == pytest.approx(11.1)
    assert finals[1].start == pytest.approx(11.2)
    assert finals[1].end == pytest.approx(12.0)
    assert all(s.provider == "faster-whisper" and s.confidence == 0.9 for s in finals)
    assert await p.finish() == []


async def test_ongoing_speech_yields_a_partial_about_every_second() -> None:
    backend = ScriptedBackend()
    p = provider(backend)
    data = pcm((0, 0.2), (1, 1.0), (2, 1.0), (3, 1.0))

    out = await feed_all(p, chunks(data))

    assert backend.vad_calls == 3  # 3.2 s of audio, one step per second
    assert all(not s.is_final for s in out)
    assert [s.text for s in out] == ["hola", "hola clase", "hola clase derivadas"]
    assert out[-1].start == pytest.approx(0.2)
    assert out[-1].end == pytest.approx(3.0)


async def test_finish_commits_the_speech_still_going_on() -> None:
    p = provider(ScriptedBackend())
    await feed_all(p, chunks(pcm((0, 0.3), (3, 1.2))))

    tail = await p.finish()

    assert [(s.text, s.is_final) for s in tail] == [("derivadas", True)]
    assert tail[0].start == pytest.approx(0.3)
    assert tail[0].end == pytest.approx(1.5)
    with pytest.raises(RuntimeError):
        await p.feed(chunks(pcm((0, 0.1)))[0])


async def test_an_utterance_past_the_maximum_is_committed_without_waiting_for_silence() -> None:
    p = provider(ScriptedBackend(), max_utterance_seconds=2.0)

    out = await feed_all(p, chunks(pcm((1, 1.0), (2, 1.0), (4, 0.5))))
    finals = [s.text for s in out if s.is_final]

    assert finals == ["hola", "clase"]
    assert [s.text for s in await p.finish()] == ["integral"]


async def test_silence_is_trimmed_so_old_audio_is_never_retranscribed() -> None:
    backend = ScriptedBackend()
    p = provider(backend)
    out = await feed_all(p, chunks(pcm((0, 30.0), (1, 0.5), (0, 1.5))))

    assert [s.text for s in out if s.is_final] == ["hola"]
    assert max(backend.transcribed) < 2.0
    assert len(p._audio) <= RATE  # only the leading pad stays buffered


async def test_inference_runs_in_a_worker_thread() -> None:
    backend = ScriptedBackend()
    p = provider(backend)
    await feed_all(p, chunks(pcm((1, 1.0), (0, 1.0))))
    await p.finish()

    assert backend.threads
    assert threading.get_ident() not in backend.threads


async def test_a_long_gap_in_the_stream_commits_what_came_before() -> None:
    p = provider(ScriptedBackend())
    first = await feed_all(p, chunks(pcm((1, 0.5)), start=0.0))
    later = await feed_all(p, chunks(pcm((0, 0.2), (2, 0.5)), start=10.0))
    tail = await p.finish()

    assert [s.text for s in first + later if s.is_final] == ["hola"]
    assert [(s.text, s.start) for s in tail] == [("clase", pytest.approx(10.2))]


async def test_a_short_gap_is_filled_with_silence() -> None:
    p = provider(ScriptedBackend())
    out = await feed_all(p, chunks(pcm((1, 0.5)), start=0.0))
    out += await feed_all(p, chunks(pcm((2, 0.5)), start=1.5))
    out += await p.finish()

    finals = [(s.text, round(s.start, 3), round(s.end, 3)) for s in out if s.is_final]
    assert finals == [("hola", 0.0, 0.5), ("clase", 1.5, 2.0)]


async def test_audio_in_another_format_is_refused() -> None:
    p = provider(ScriptedBackend())
    with pytest.raises(ValueError, match="16000"):
        await p.feed(AudioChunk(start=0, data=b"\0\0" * 800, sample_rate=8000))


async def test_behind_the_buffered_provider_the_gateway_gets_every_final() -> None:
    buffered = BufferedProvider(provider(ScriptedBackend()))
    out: list[NormalisedSegment] = []
    for piece in chunks(pcm((1, 0.5), (0, 1.0), (2, 0.7))):
        out.extend(await buffered.feed(piece))
    out.extend(await asyncio.wait_for(buffered.finish(), timeout=5))

    assert [s.text for s in out if s.is_final] == ["hola", "clase"]


def test_the_registry_builds_it_without_importing_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_faster_whisper(monkeypatch)
    settings = SttSettings(
        mode="server",
        provider="faster-whisper",
        options={
            "faster-whisper": {"model": "small", "device": "cpu", "partial_interval_seconds": 2}
        },
    )

    p = provider_from_settings(settings)

    assert isinstance(p, FasterWhisperProvider)
    assert isinstance(p.backend, FasterWhisperBackend)
    assert p.backend.model_name == "small"
    assert p.backend.language == "es"
    assert p.partial_interval == 2.0
    assert not p.backend.loaded
    assert isinstance(get_provider("faster-whisper"), FasterWhisperProvider)


def test_importing_the_stt_package_loads_no_concrete_provider() -> None:
    code = (
        "import sys, studentassistant.stt; "
        "assert 'studentassistant.stt.faster_whisper' not in sys.modules; "
        "assert 'faster_whisper' not in sys.modules"
    )
    import subprocess

    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


# --- the backend over a stand-in faster_whisper -----------------------------------------------


class FakeSegment(types.SimpleNamespace):
    pass


class FakeWhisperModel:
    instances: list[FakeWhisperModel] = []
    cuda_broken = False

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(kwargs)
        if self.kwargs["device"] == "cuda" and FakeWhisperModel.cuda_broken:
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
        segments = [
            FakeSegment(start=0.0, end=1.0, text=" Hola. ", avg_logprob=-0.1, no_speech_prob=0.1),
            FakeSegment(start=1.0, end=1.5, text="  ", avg_logprob=-0.1, no_speech_prob=0.1),
            FakeSegment(start=1.5, end=2.0, text="gracias", avg_logprob=-1.5, no_speech_prob=0.9),
        ]
        return iter(segments), None


@pytest.fixture
def fake_whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = install_fakes(monkeypatch, tmp_path / "hf", cuda_devices=1)
    FakeWhisperModel.instances = []
    FakeWhisperModel.cuda_broken = False
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    vad = types.ModuleType("faster_whisper.vad")
    vad.VadOptions = lambda **kw: kw  # type: ignore[attr-defined]
    vad.calls = []  # type: ignore[attr-defined]

    def get_speech_timestamps(audio: np.ndarray, options: Any, rate: int) -> list[dict[str, int]]:
        vad.calls.append((options, rate))  # type: ignore[attr-defined]
        return [{"start": 100, "end": 900}]

    vad.get_speech_timestamps = get_speech_timestamps  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    return module


def test_the_backend_loads_lazily_on_cuda_with_int8_float16(fake_whisper: Any) -> None:
    backend = FasterWhisperBackend(download_root="/models", min_silence_ms=700)
    assert FakeWhisperModel.instances == []

    spans = backend.speech_spans(np.zeros(1000, dtype=np.float32))
    recognised = backend.transcribe(np.zeros(1000, dtype=np.float32))
    backend.transcribe(np.zeros(1000, dtype=np.float32))

    (model,) = FakeWhisperModel.instances
    assert model.model == "large-v3-turbo"
    assert model.kwargs == {
        "device": "cuda",
        "compute_type": "int8_float16",
        "download_root": "/models",
    }
    assert model.calls[0]["language"] == "es"
    assert model.calls[0]["vad_filter"] is False
    assert spans == [(100, 900)]
    assert sys.modules["faster_whisper.vad"].calls[0] == (
        {"threshold": 0.5, "min_silence_duration_ms": 700},
        16_000,
    )
    # Blank and hallucinated (no speech + low log-probability) segments are dropped.
    assert [r.text for r in recognised] == ["Hola."]
    assert recognised[0].confidence == pytest.approx(0.905, abs=1e-3)


def test_auto_falls_back_to_the_cpu_when_a_cuda_library_is_missing(fake_whisper: Any) -> None:
    FakeWhisperModel.cuda_broken = True
    backend = FasterWhisperBackend()

    recognised = backend.transcribe(np.zeros(1000, dtype=np.float32))

    assert [r.text for r in recognised] == ["Hola."]
    assert [m.kwargs["device"] for m in FakeWhisperModel.instances] == ["cuda", "cpu"]
    assert (backend.device, backend.compute_type) == ("cpu", "int8")


def test_an_explicit_cuda_device_does_not_fall_back(fake_whisper: Any) -> None:
    FakeWhisperModel.cuda_broken = True
    backend = FasterWhisperBackend(device="cuda")
    with pytest.raises(RuntimeError, match="libcublas"):
        backend.transcribe(np.zeros(1000, dtype=np.float32))


def test_device_auto_falls_back_to_cpu_int8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fakes(monkeypatch, tmp_path, cuda_devices=0)
    assert resolve_device("auto") == ("cpu", "int8")
    assert resolve_device("cuda") == ("cuda", "int8_float16")
    assert resolve_device("cpu", "float32") == ("cpu", "float32")


def test_a_missing_extra_is_reported_when_the_model_is_first_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_faster_whisper(monkeypatch)
    backend = FasterWhisperBackend(device="cpu")
    with pytest.raises(WhisperNotInstalledError, match="--extra whisper"):
        backend.transcribe(np.zeros(10, dtype=np.float32))


# --- `studentassistant stt download` ----------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "conf" / "config.toml"))
    return tmp_path


def test_stt_download_fetches_the_configured_model(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fakes(monkeypatch, env / "hf")
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "faster-whisper")

    result = CliRunner().invoke(cli, ["stt", "download"])

    assert result.exit_code == 0, result.output
    assert "Aviso" not in result.output
    assert "Modelo de Whisper large-v3-turbo listo en" in result.output
    assert fake.calls == [{"model": "large-v3-turbo", "cache_dir": None, "local_files_only": False}]


def test_stt_download_warns_in_client_mode_and_fails_without_the_extra(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_faster_whisper(monkeypatch)

    result = CliRunner().invoke(cli, ["stt", "download"])

    assert result.exit_code == 1
    assert "Aviso: la voz usa ahora el modo client" in result.output
    assert "No se pudo descargar el modelo: faster-whisper no está instalado" in result.output


# --- the real model -----------------------------------------------------------------------------


@pytest.mark.integration
async def test_real_model_transcribes_a_short_spanish_wav() -> None:
    """Needs the `whisper` extra, the model and a Spanish WAV (16 kHz mono PCM16):
    `SA_TEST_SPANISH_WAV=<path>` and optionally `SA_TEST_SPANISH_WORDS="palabra otra"` (words
    the transcript must contain, lower case)."""
    path = os.environ.get("SA_TEST_SPANISH_WAV")
    if not path:
        pytest.skip("set SA_TEST_SPANISH_WAV to a short Spanish 16 kHz mono PCM16 WAV")
    pytest.importorskip("faster_whisper")
    with wave.open(path, "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2)
        data = wav.readframes(wav.getnframes())
    p = FasterWhisperProvider({"device": os.environ.get("SA_TEST_WHISPER_DEVICE", "auto")})

    out = await feed_all(p, chunks(data, seconds=0.1))
    out.extend(await p.finish())
    finals = [s for s in out if s.is_final]
    text = " ".join(s.text for s in finals).lower()

    assert finals, out
    assert all(0 <= s.start <= s.end <= len(data) / 2 / RATE + 0.5 for s in finals)
    for word in os.environ.get("SA_TEST_SPANISH_WORDS", "").split():
        assert word in text, text
