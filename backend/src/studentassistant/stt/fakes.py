"""Test doubles for the STT layer: a scripted server-side provider and a scripted client source.

`FakeProvider` is registered under the name `fake` (entry point group
`studentassistant.stt_providers`), so `stt.mode = "server"`, `stt.provider = "fake"` works in
tests of any module without a model, a GPU or the network.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.config import DEFAULT_STT_LANGUAGE
from studentassistant.stt.models import AudioChunk, ClientSegment, NormalisedSegment
from studentassistant.stt.provider import ProviderState, ProviderStatus, SpeechToTextProvider
from studentassistant.stt.sink import TranscriptSink


class ScriptedSegment(BaseModel):
    """What `FakeProvider` "hears" in the audio span `start`..`end` (session seconds)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: float
    end: float
    text: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_final: bool = True


class FakeProvider(SpeechToTextProvider):
    """Yields each scripted segment as soon as the audio fed so far reaches the segment's end.

    The script comes from `segments=` or from the options table's `segments` list (so
    `[stt.options.fake]` in a test config works too). `finish` yields whatever the audio never
    reached. Every chunk fed is kept in `fed`. `set_status` scripts its `status` (default
    `idle`), e.g. a cloud provider losing its connection.
    """

    name = "fake"

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        language: str = DEFAULT_STT_LANGUAGE,
        segments: Iterable[ScriptedSegment | Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(options, language=language)
        script = segments if segments is not None else self.options.get("segments", [])
        self._pending = sorted(
            (ScriptedSegment.model_validate(s) for s in script), key=lambda s: (s.end, s.start)
        )
        self.fed: list[AudioChunk] = []
        self.finished = False
        self._status = ProviderStatus("idle")

    @property
    def status(self) -> ProviderStatus:
        return self._status

    def set_status(self, state: ProviderState, detail: str | None = None) -> None:
        """What `status` reports from now on."""
        self._status = ProviderStatus(state, detail)

    def _normalise(self, scripted: ScriptedSegment) -> NormalisedSegment:
        return NormalisedSegment(provider=self.name, **scripted.model_dump())

    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        if self.finished:
            raise RuntimeError("FakeProvider was fed after finish()")
        self.fed.append(chunk)
        heard_until = max(c.end for c in self.fed)
        ready = [s for s in self._pending if s.end <= heard_until]
        self._pending = [s for s in self._pending if s.end > heard_until]
        return [self._normalise(s) for s in ready]

    async def finish(self) -> list[NormalisedSegment]:
        self.finished = True
        rest, self._pending = self._pending, []
        return [self._normalise(s) for s in rest]


class ScriptedClientSource:
    """The segments a capture client's recognizer would send, in order, for sink tests."""

    def __init__(self, segments: Sequence[ClientSegment | Mapping[str, Any]]) -> None:
        self.segments = [ClientSegment.model_validate(s) for s in segments]

    def __iter__(self) -> Iterator[ClientSegment]:
        return iter(self.segments)

    async def play_into(self, sink: TranscriptSink) -> list[NormalisedSegment]:
        """Ingest every scripted segment into `sink`; return what it produced."""
        return [await sink.ingest(segment) for segment in self.segments]
