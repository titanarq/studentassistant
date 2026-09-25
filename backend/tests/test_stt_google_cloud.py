"""`GoogleCloudSpeechProvider`: streaming partials/finals, stream restarts, errors as a status.

No test here reaches Google: a scripted `SpeechStreamClient` answers from the audio it consumes,
and the `GoogleSpeechClient` adapter is tested over a stand-in `google.cloud.speech` module. The
real API is `integration` only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from studentassistant.config import SttSettings
from studentassistant.stt import AudioChunk, BufferedProvider, get_provider, provider_from_settings
from studentassistant.stt.google_cloud import (
    GoogleCloudSetupError,
    GoogleCloudSpeechProvider,
    GoogleSpeechClient,
    StreamResult,
    language_code,
)

pytestmark = pytest.mark.anyio

RATE = 16_000
TIMEOUT = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def silence(seconds: float) -> bytes:
    return bytes(round(seconds * RATE) * 2)


def chunk(start: float, seconds: float) -> AudioChunk:
    return AudioChunk(start=start, data=silence(seconds))


@dataclass
class ScriptedClient:
    """Yields each scripted `(audio seconds, result)` once the stream has consumed that much
    audio (the rest when the audio ends); one script per stream, in order."""

    scripts: list[list[tuple[float, StreamResult | Exception]]] = field(default_factory=list)
    received: list[float] = field(default_factory=list)  # seconds of audio per stream
    rates: list[int] = field(default_factory=list)
    gate: threading.Event | None = None

    def stream(self, audio: Iterator[bytes], *, sample_rate: int) -> Iterator[StreamResult]:
        index = len(self.received)
        self.received.append(0.0)
        self.rates.append(sample_rate)
        script = list(self.scripts[index]) if index < len(self.scripts) else []
        for data in audio:
            if self.gate is not None:
                assert self.gate.wait(TIMEOUT)
            self.received[index] += len(data) / (sample_rate * 2)
            while script and script[0][0] <= self.received[index] + 1e-9:
                yield from _emit(script.pop(0)[1])
        for _, item in script:
            yield from _emit(item)


def _emit(item: StreamResult | Exception) -> Iterator[StreamResult]:
    if isinstance(item, Exception):
        raise item
    yield item


async def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def spans(segments: list[Any]) -> list[tuple[float, float, str, bool]]:
    return [(round(s.start, 3), round(s.end, 3), s.text, s.is_final) for s in segments]


async def test_partials_and_finals_come_out_in_session_time() -> None:
    client = ScriptedClient(
        scripts=[
            [
                (0.5, StreamResult("hola", is_final=False, end=0.5)),
                (1.0, StreamResult("Hola, clase.", is_final=True, end=1.0, confidence=0.93)),
                (1.5, StreamResult("derivadas", is_final=False, end=1.5)),
                (2.0, StreamResult("Derivadas.", is_final=True, end=2.0, confidence=1.7)),
            ]
        ]
    )
    p = GoogleCloudSpeechProvider(client=client)
    out = []
    for i in range(4):
        out.extend(await p.feed(chunk(3.0 + i * 0.5, 0.5)))
    out.extend(await p.finish())

    assert spans(out) == [
        (3.0, 3.5, "hola", False),
        (3.0, 4.0, "Hola, clase.", True),
        (4.0, 4.5, "derivadas", False),
        (4.0, 5.0, "Derivadas.", True),
    ]
    assert {s.provider for s in out} == {"google-cloud"}
    assert [s.confidence for s in out if s.is_final] == [0.93, 1.0]
    assert p.status.state == "streaming" and p.status.detail is None


async def test_feed_never_waits_for_the_network() -> None:
    gate = threading.Event()
    client = ScriptedClient(scripts=[[(0.5, StreamResult("hola", is_final=True, end=0.5))]])
    client.gate = gate
    p = GoogleCloudSpeechProvider(client=client)

    assert await asyncio.wait_for(p.feed(chunk(0.0, 0.5)), 1.0) == []
    gate.set()
    out = await p.finish()

    assert spans(out) == [(0.0, 0.5, "hola", True)]


async def test_results_are_clamped_to_the_audio_sent_and_blank_ones_skipped() -> None:
    client = ScriptedClient(
        scripts=[
            [
                (0.5, StreamResult("", is_final=False, end=0.5)),
                (0.5, StreamResult("uno", is_final=True, end=9.0)),
                (1.0, StreamResult("dos", is_final=True, end=None)),
            ]
        ]
    )
    p = GoogleCloudSpeechProvider(client=client)
    out = await p.feed(chunk(0.0, 0.5))
    out += await p.feed(chunk(0.5, 0.5))
    out += await p.finish()

    assert spans(out) == [(0.0, 0.5, "uno", True), (0.5, 1.0, "dos", True)]


async def test_a_short_gap_is_filled_with_silence_and_a_long_one_opens_a_new_stream() -> None:
    client = ScriptedClient(
        scripts=[
            [(2.0, StreamResult("uno", is_final=True, end=2.0))],
            [(0.5, StreamResult("dos", is_final=True, end=0.5))],
        ]
    )
    p = GoogleCloudSpeechProvider(client=client)
    out = await p.feed(chunk(0.0, 0.5))
    out += await p.feed(chunk(1.5, 0.5))  # 1 s gap: padded
    out += await p.feed(chunk(10.0, 0.5))  # 8 s gap: new stream from 10.0
    out += await p.finish()

    assert client.received == [2.0, 0.5]
    assert sorted(spans(out)) == [(0.0, 2.0, "uno", True), (10.0, 10.5, "dos", True)]


async def test_a_new_stream_opens_before_the_stream_limit() -> None:
    client = ScriptedClient(
        scripts=[
            [(1.0, StreamResult("a", is_final=True, end=1.0))],
            [(1.0, StreamResult("b", is_final=True, end=1.0))],
            [(0.5, StreamResult("c", is_final=True, end=0.5))],
        ]
    )
    p = GoogleCloudSpeechProvider({"stream_limit_seconds": 1.0}, client=client)
    out = []
    for i in range(5):
        out += await p.feed(chunk(i * 0.5, 0.5))
    out += await p.finish()

    assert client.received == [1.0, 1.0, 0.5]
    assert sorted(spans(out)) == [
        (0.0, 1.0, "a", True),
        (1.0, 2.0, "b", True),
        (2.0, 2.5, "c", True),
    ]


async def test_a_new_sample_rate_opens_a_new_stream() -> None:
    client = ScriptedClient()
    p = GoogleCloudSpeechProvider(client=client)
    await p.feed(chunk(0.0, 0.5))
    await p.feed(AudioChunk(start=0.5, data=bytes(8000 * 2 // 2), sample_rate=8000))
    await p.finish()

    assert client.rates == [16_000, 8000]


async def test_a_failed_stream_degrades_to_reconnecting_then_recovers() -> None:
    now = [100.0]
    client = ScriptedClient(
        scripts=[
            [(0.5, RuntimeError("UNAVAILABLE: connection reset"))],
            [(0.5, StreamResult("otra vez", is_final=True, end=0.5))],
        ]
    )
    p = GoogleCloudSpeechProvider({"retry_seconds": 5}, client=client, clock=lambda: now[0])
    await p.feed(chunk(0.0, 0.5))
    await wait_until(lambda: p.status.state == "reconnecting")
    assert "Google Cloud" in (p.status.detail or "") and "5 s" in (p.status.detail or "")

    assert await p.feed(chunk(0.5, 0.5)) == []  # inside the retry delay: dropped
    assert p.dropped_seconds == 0.5
    now[0] += 5.0
    out = await p.feed(chunk(1.0, 0.5))  # a new stream from 1.0
    out += await p.finish()

    assert client.received == [0.5, 0.5]
    assert spans(out) == [(1.0, 1.5, "otra vez", True)]
    assert p.status.state == "streaming" and p.status.detail is None


async def test_a_setup_failure_makes_it_unavailable_and_drops_the_audio() -> None:
    client = ScriptedClient(scripts=[[(0.0, GoogleCloudSetupError("no credentials"))]])
    p = GoogleCloudSpeechProvider(client=client)
    await p.feed(chunk(0.0, 0.5))
    await wait_until(lambda: p.status.state == "unavailable")

    assert await p.feed(chunk(0.5, 0.5)) == []
    assert await p.finish() == []
    assert len(client.received) == 1
    assert p.dropped_seconds == 0.5
    assert "credenciales" in (p.status.detail or "")


async def test_finish_is_bounded_when_the_stream_hangs() -> None:
    class Hanging:
        def stream(self, audio: Iterator[bytes], *, sample_rate: int) -> Iterator[StreamResult]:
            next(audio)
            threading.Event().wait(TIMEOUT)
            return iter(())

    p = GoogleCloudSpeechProvider({"finish_timeout_seconds": 0.2}, client=Hanging())
    await p.feed(chunk(0.0, 0.5))
    started = time.monotonic()
    assert await p.finish() == []
    assert time.monotonic() - started < 2.0


async def test_fed_after_finish_or_with_8_bit_audio_raises() -> None:
    p = GoogleCloudSpeechProvider(client=ScriptedClient())
    with pytest.raises(ValueError, match="16-bit"):
        await p.feed(AudioChunk(start=0.0, data=b"\0" * 10, sample_width=1))
    await p.finish()
    with pytest.raises(RuntimeError):
        await p.feed(chunk(0.0, 0.1))


async def test_errors_never_reach_the_buffered_gateway_path() -> None:
    client = ScriptedClient(scripts=[[(0.1, RuntimeError("boom"))]])
    p = BufferedProvider(GoogleCloudSpeechProvider(client=client))
    for i in range(3):
        assert await p.feed(chunk(i * 0.1, 0.1)) == []
    assert await p.finish() == []


def test_registered_under_google_cloud_without_importing_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "google.cloud.speech", raising=False)
    settings = SttSettings(
        mode="server",
        provider="google-cloud",
        options={"google-cloud": {"model": "latest_short", "phrases": ["derivada"]}},
    )
    p = provider_from_settings(settings)

    assert isinstance(p, GoogleCloudSpeechProvider)
    assert isinstance(p.client, GoogleSpeechClient)
    assert (p.client.language_code, p.client.model, p.client.phrases) == (
        "es-ES",
        "latest_short",
        ["derivada"],
    )
    assert "google.cloud.speech" not in sys.modules
    assert isinstance(get_provider("google-cloud", language="en"), GoogleCloudSpeechProvider)


def test_language_code() -> None:
    assert language_code("es") == "es-ES"
    assert language_code("es-MX") == "es-MX"
    assert language_code("fr") == "fr"


def test_bad_timing_options_are_refused() -> None:
    with pytest.raises(ValueError):
        GoogleCloudSpeechProvider({"stream_limit_seconds": 0}, client=ScriptedClient())


# --- the adapter over a stand-in google.cloud.speech ------------------------------------------


class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeSpeechModule(types.ModuleType):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__("google.cloud.speech")
        module = self
        self.responses = responses
        self.calls: dict[str, Any] = {}

        class RecognitionConfig(_Obj):
            AudioEncoding = types.SimpleNamespace(LINEAR16="LINEAR16")

        class SpeechClient:
            def __init__(self) -> None:
                module.calls["credentials_file"] = None

            @classmethod
            def from_service_account_file(cls, path: str) -> SpeechClient:
                if not Path(path).exists():
                    raise FileNotFoundError(path)
                client = cls()
                module.calls["credentials_file"] = path
                return client

            def streaming_recognize(self, config: Any, requests: Any) -> Iterator[Any]:
                module.calls["config"] = config
                module.calls["audio"] = [r.audio_content for r in requests]
                return iter(module.responses)

        self.RecognitionConfig = RecognitionConfig
        self.SpeechContext = _Obj
        self.StreamingRecognitionConfig = _Obj
        self.StreamingRecognizeRequest = _Obj
        self.SpeechClient = SpeechClient


def response(*results: Any, error: Any = None) -> Any:
    return _Obj(results=list(results), error=error or _Obj(code=0, message=""))


def result(text: str, *, final: bool, end: float, confidence: float = 0.0) -> Any:
    return _Obj(
        alternatives=[_Obj(transcript=text, confidence=confidence)],
        is_final=final,
        result_end_time=timedelta(seconds=end),
    )


def test_adapter_builds_the_config_and_maps_the_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "key.json"
    key.write_text("{}")
    fake = FakeSpeechModule(
        [
            response(result(" Hola", final=False, end=0.4), result(" cla", final=False, end=0.6)),
            response(result(" Hola, clase.", final=True, end=1.2, confidence=0.9)),
            response(_Obj(alternatives=[], is_final=False, result_end_time=None)),
        ]
    )
    monkeypatch.setitem(sys.modules, "google.cloud.speech", fake)
    client = GoogleSpeechClient(
        language_code="es-ES", model="latest_long", credentials_file=str(key), phrases=["clase"]
    )

    out = list(client.stream(iter([b"ab", b"cd"]), sample_rate=RATE))

    assert out == [
        StreamResult("Hola cla", is_final=False, end=0.6),
        StreamResult("Hola, clase.", is_final=True, end=1.2, confidence=0.9),
    ]
    config = fake.calls["config"]
    assert config.interim_results is True
    assert (config.config.language_code, config.config.sample_rate_hertz) == ("es-ES", RATE)
    assert config.config.encoding == "LINEAR16"
    assert config.config.speech_contexts[0].phrases == ["clase"]
    assert fake.calls["audio"] == [b"ab", b"cd"]
    assert fake.calls["credentials_file"] == str(key)


def test_adapter_raises_on_an_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSpeechModule([response(error=_Obj(code=11, message="Audio Timeout Error"))])
    monkeypatch.setitem(sys.modules, "google.cloud.speech", fake)

    with pytest.raises(RuntimeError, match="Audio Timeout"):
        list(GoogleSpeechClient(language_code="es-ES").stream(iter([b""]), sample_rate=RATE))


def test_adapter_setup_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "google.cloud.speech", FakeSpeechModule([]))
    missing = GoogleSpeechClient(language_code="es-ES", credentials_file=str(tmp_path / "no"))
    with pytest.raises(GoogleCloudSetupError, match="credentials"):
        list(missing.stream(iter([]), sample_rate=RATE))

    monkeypatch.setitem(sys.modules, "google.cloud.speech", None)
    with pytest.raises(GoogleCloudSetupError, match="uv sync --extra google-cloud"):
        list(GoogleSpeechClient(language_code="es-ES").stream(iter([]), sample_rate=RATE))


@pytest.mark.integration
async def test_real_api_transcribes_a_short_spanish_wav() -> None:
    """Needs the `google-cloud` extra, credentials (`SA_TEST_GOOGLE_CREDENTIALS=<key.json>` or
    Application Default Credentials with `SA_TEST_GOOGLE_CLOUD=1`) and a Spanish WAV (16 kHz mono
    PCM16): `SA_TEST_SPANISH_WAV=<path>`, optionally `SA_TEST_SPANISH_WORDS="palabra otra"`."""
    path = os.environ.get("SA_TEST_SPANISH_WAV")
    key = os.environ.get("SA_TEST_GOOGLE_CREDENTIALS")
    if not path or not (key or os.environ.get("SA_TEST_GOOGLE_CLOUD")):
        pytest.skip("set SA_TEST_SPANISH_WAV and SA_TEST_GOOGLE_CREDENTIALS/SA_TEST_GOOGLE_CLOUD")
    pytest.importorskip("google.cloud.speech")
    with wave.open(path, "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2)
        data = wav.readframes(wav.getnframes())
    p = GoogleCloudSpeechProvider({"credentials_file": key} if key else {})
    step = RATE * 2 // 10
    out = []
    for i in range(0, len(data), step):
        out.extend(await p.feed(AudioChunk(start=i / (RATE * 2), data=data[i : i + step])))
        await asyncio.sleep(0.1)  # real time
    out.extend(await p.finish())

    text = " ".join(s.text for s in out if s.is_final).lower()
    assert text, p.status
    for word in os.environ.get("SA_TEST_SPANISH_WORDS", "").split():
        assert word in text
