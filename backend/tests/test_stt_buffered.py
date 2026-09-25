"""`BufferedProvider`: feed never waits, partials give way under backlog, finals always arrive."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from studentassistant.config import (
    DEFAULT_STT_MAX_BACKLOG_SECONDS,
    Settings,
    SttSettings,
)
from studentassistant.stt import (
    AudioChunk,
    BufferedProvider,
    NormalisedSegment,
    SpeechToTextProvider,
    buffered_provider_from_settings,
)
from studentassistant.stt.fakes import FakeProvider

pytestmark = pytest.mark.anyio

SECOND = b"\0\0" * 16_000  # one second of PCM16 16 kHz mono


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def chunk(start: float, seconds: float = 1.0) -> AudioChunk:
    return AudioChunk(start=start, data=SECOND[: int(len(SECOND) * seconds)])


class GatedProvider(SpeechToTextProvider):
    """A deliberately slow provider: each `feed` waits for the test to open the gate.

    Every chunk yields a partial of its span, and each second chunk also a final.
    """

    name = "gated"

    def __init__(self) -> None:
        super().__init__({"model": "x"}, language="es")
        self.gate = asyncio.Event()
        self.fed: list[AudioChunk] = []
        self.finished = False

    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        await self.gate.wait()
        self.fed.append(chunk)
        index = len(self.fed)
        partial = NormalisedSegment(
            start=chunk.start, end=chunk.end, text=f"p{index}", provider=self.name, is_final=False
        )
        if index % 2:
            return [partial]
        final = NormalisedSegment(
            start=chunk.start, end=chunk.end, text=f"f{index}", provider=self.name
        )
        return [partial, final]

    async def finish(self) -> list[NormalisedSegment]:
        self.finished = True
        return [NormalisedSegment(start=99.0, end=100.0, text="tail", provider=self.name)]


class FailingProvider(GatedProvider):
    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        if chunk.start == 1.0:
            raise RuntimeError("model crashed")
        self.gate.set()
        return await super().feed(chunk)


async def test_it_is_a_provider_with_the_wrapped_language_and_options() -> None:
    inner = GatedProvider()
    buffered = BufferedProvider(inner)

    assert isinstance(buffered, SpeechToTextProvider)
    assert buffered.inner is inner
    assert buffered.language == "es"
    assert buffered.options == {"model": "x"}
    assert buffered.max_backlog_seconds == DEFAULT_STT_MAX_BACKLOG_SECONDS


async def test_feed_returns_without_waiting_for_inference() -> None:
    inner = GatedProvider()
    buffered = BufferedProvider(inner)

    for i in range(3):
        assert await asyncio.wait_for(buffered.feed(chunk(float(i))), timeout=0.5) == []

    assert inner.fed == []
    assert buffered.backlog_seconds == pytest.approx(3.0)
    inner.gate.set()
    rest = await buffered.finish()
    assert [s.text for s in rest] == ["p1", "p2", "f2", "p3", "tail"]
    assert buffered.backlog_seconds == 0.0


async def test_feed_returns_what_the_provider_produced_since_the_last_call() -> None:
    script = [
        {"start": 0.0, "end": 0.5, "text": "la", "is_final": False},
        {"start": 0.0, "end": 0.9, "text": "la aceleración"},
        {"start": 1.0, "end": 1.9, "text": "es constante"},
        {"start": 5.0, "end": 6.0, "text": "nunca oído"},
    ]
    inner = FakeProvider(segments=script)
    buffered = BufferedProvider(inner)

    produced: list[NormalisedSegment] = []
    for i in range(3):
        produced += await buffered.feed(chunk(float(i)))
    for _ in range(5):  # let the background task catch up
        await asyncio.sleep(0)
    produced += await buffered.feed(chunk(3.0))

    assert [s.text for s in produced] == ["la", "la aceleración", "es constante"]
    assert [c.start for c in inner.fed] == [0.0, 1.0, 2.0, 3.0]
    assert [s.text for s in await buffered.finish()] == ["nunca oído"]
    assert inner.finished


async def test_superseded_partials_are_dropped_under_backlog_but_finals_never() -> None:
    inner = GatedProvider()
    buffered = BufferedProvider(inner, max_backlog_seconds=2.0)
    for i in range(6):
        await buffered.feed(chunk(float(i)))
    assert buffered.backlog_seconds > 2.0

    inner.gate.set()
    for _ in range(4):  # the first chunks go through while the backlog is still large
        await asyncio.sleep(0)
    early = await buffered.feed(chunk(6.0))
    rest = await buffered.finish()
    texts = [s.text for s in early + rest]

    assert [t for t in texts if t.startswith("f")] == ["f2", "f4", "f6"]
    assert buffered.dropped > 0
    assert len([t for t in texts if t.startswith("p")]) == 7 - buffered.dropped
    assert texts[-1] == "tail"


async def test_no_partial_is_dropped_within_the_backlog_bound() -> None:
    inner = GatedProvider()
    inner.gate.set()
    buffered = BufferedProvider(inner, max_backlog_seconds=60.0)
    produced: list[NormalisedSegment] = []
    for i in range(4):
        produced += await buffered.feed(chunk(float(i)))

    texts = [s.text for s in produced + await buffered.finish()]

    assert buffered.dropped == 0
    assert texts == ["p1", "p2", "f2", "p3", "p4", "f4", "tail"]


async def test_finish_drains_the_queue_in_order_and_feed_after_finish_fails() -> None:
    inner = GatedProvider()
    buffered = BufferedProvider(inner)
    for i in range(4):
        await buffered.feed(chunk(float(i)))

    async def open_gate() -> None:
        await asyncio.sleep(0.01)
        inner.gate.set()

    opener = asyncio.create_task(open_gate())
    rest = await buffered.finish()
    await opener

    assert [c.start for c in inner.fed] == [0.0, 1.0, 2.0, 3.0]
    assert [s.text for s in rest if s.is_final] == ["f2", "f4", "tail"]
    assert inner.finished
    with pytest.raises(RuntimeError, match="after finish"):
        await buffered.feed(chunk(4.0))


async def test_a_failing_chunk_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    inner = FailingProvider()
    buffered = BufferedProvider(inner)
    produced: list[NormalisedSegment] = []
    for i in range(3):
        produced += await buffered.feed(chunk(float(i)))

    produced += await buffered.finish()

    assert [c.start for c in inner.fed] == [0.0, 2.0]
    assert [s.text for s in produced] == ["p1", "p2", "f2", "tail"]
    assert "failed on a chunk" in caplog.text


async def test_the_factory_wraps_the_configured_provider() -> None:
    settings = SttSettings(
        mode="server",
        provider="fake",
        max_backlog_seconds=3.5,
        options={"fake": {"segments": [{"start": 0.0, "end": 0.5, "text": "hola"}]}},
    )

    buffered = buffered_provider_from_settings(settings)

    assert isinstance(buffered.inner, FakeProvider)
    assert buffered.max_backlog_seconds == 3.5
    assert [s.provider for s in await buffered.finish()] == ["fake"]


def test_the_backlog_bound_is_configurable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("SA_STT__MAX_BACKLOG_SECONDS", raising=False)
    assert Settings().stt.max_backlog_seconds == DEFAULT_STT_MAX_BACKLOG_SECONDS

    monkeypatch.setenv("SA_STT__MAX_BACKLOG_SECONDS", "4.5")
    assert Settings().stt.max_backlog_seconds == 4.5


def test_vocabulary_hints_pass_through_to_the_wrapped_provider() -> None:
    inner = FakeProvider()
    buffered = BufferedProvider(inner)
    assert buffered.vocabulary == inner.vocabulary == ()

    buffered.set_vocabulary(["Historia", "caciquismo"])

    assert inner.vocabulary == ("Historia", "caciquismo")
    assert buffered.vocabulary == ("Historia", "caciquismo")
