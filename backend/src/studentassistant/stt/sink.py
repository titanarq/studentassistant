"""Where client-side recognizer segments enter the backend (`stt.mode = client`, ADR-0008)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from studentassistant.config import SttSettings
from studentassistant.stt.models import ClientSegment, NormalisedSegment


class TranscriptSink(ABC):
    """Ingests the `ClientSegment`s a capture client sends and turns them into `NormalisedSegment`s.

    `provider` is the client recognizer's name (`stt.provider`, e.g. `web-speech`); every segment
    the sink produces carries it.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider

    @abstractmethod
    async def ingest(self, segment: ClientSegment) -> NormalisedSegment:
        """Take one client segment; return it normalised into session time."""


class InMemoryTranscriptSink(TranscriptSink):
    """Keeps every normalised segment in `segments`, in arrival order.

    Session time is client time minus `clock_offset` (the client-clock reading at session start);
    real alignment, dedupe and persistence belong to the transcript pipeline.
    """

    def __init__(self, provider: str, *, clock_offset: float = 0.0) -> None:
        super().__init__(provider)
        self.clock_offset = clock_offset
        self.segments: list[NormalisedSegment] = []

    @classmethod
    def from_settings(cls, settings: SttSettings, *, clock_offset: float = 0.0) -> Self:
        return cls(settings.provider, clock_offset=clock_offset)

    async def ingest(self, segment: ClientSegment) -> NormalisedSegment:
        normalised = NormalisedSegment(
            start=max(0.0, segment.client_start - self.clock_offset),
            end=max(0.0, segment.client_end - self.clock_offset),
            text=segment.text,
            provider=self.provider,
            confidence=segment.confidence,
            is_final=segment.is_final,
        )
        self.segments.append(normalised)
        return normalised
