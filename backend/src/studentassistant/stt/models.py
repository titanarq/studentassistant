"""The segments every speech-to-text path produces, and what a capture client sends in.

Both STT paths of ADR-0008 end in the same `NormalisedSegment`: a client-side recognizer's
`ClientSegment` goes through a `TranscriptSink`, server-side audio (`AudioChunk`) through a
`SpeechToTextProvider`. Nothing downstream knows which provider produced a segment.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Span(BaseModel):
    """A time span whose end never precedes its start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        start, end = self._span()
        if end < start:
            raise ValueError(f"segment ends ({end}) before it starts ({start})")
        return self

    def _span(self) -> tuple[float, float]:
        raise NotImplementedError


class NormalisedSegment(_Span):
    """One stretch of recognised speech, in session time (seconds since the session started)."""

    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str
    # The provider that produced it: `web-speech`, `android-speech`, `faster-whisper`, `fake`...
    provider: str
    # The recognizer's own confidence, when it reports one.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # A partial (interim) hypothesis may still change; a final one is what the transcript keeps.
    is_final: bool = True

    def _span(self) -> tuple[float, float]:
        return self.start, self.end


class ClientSegment(_Span):
    """A segment as a capture client's recognizer reports it, on the client's own clock.

    `client_start`/`client_end` are seconds on the client's clock; turning them into session time
    (and deduplicating partials) is the transcript pipeline's job, not this model's.
    """

    client_start: float
    client_end: float
    text: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_final: bool = True

    def _span(self) -> tuple[float, float]:
        return self.client_start, self.client_end


class AudioChunk(BaseModel):
    """A piece of the audio a client streams in `server` mode: mono PCM, starting at `start`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Session time, in seconds, of the chunk's first sample.
    start: float = Field(ge=0.0)
    data: bytes
    sample_rate: int = Field(default=16_000, gt=0)
    # Bytes per sample (2 = 16-bit PCM).
    sample_width: int = Field(default=2, gt=0)

    @property
    def duration(self) -> float:
        return len(self.data) / (self.sample_rate * self.sample_width)

    @property
    def end(self) -> float:
        return self.start + self.duration
