"""The interface every server-side speech-to-text provider implements (ADR-0008)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from studentassistant.config import DEFAULT_STT_LANGUAGE
from studentassistant.stt.models import AudioChunk, NormalisedSegment


class SpeechToTextProvider(ABC):
    """Fed the session's audio chunk by chunk, it yields `NormalisedSegment`s in session time.

    Lifecycle: construct (from the provider's options table), `feed` every chunk in order, then
    `finish` once to flush what is still buffered; the provider is not fed after `finish`.
    Heavy work (model inference) must run off the event loop, e.g. `asyncio.to_thread`.
    """

    # The name `stt.provider` selects it by, and the one its segments carry.
    name: ClassVar[str]

    def __init__(
        self, options: Mapping[str, Any] | None = None, *, language: str = DEFAULT_STT_LANGUAGE
    ) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self.language = language

    @abstractmethod
    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        """Take the next chunk; return the segments (partial or final) it made recognisable."""

    @abstractmethod
    async def finish(self) -> list[NormalisedSegment]:
        """No more audio: return every segment still pending."""
