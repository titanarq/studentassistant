"""The interface every server-side speech-to-text provider implements (ADR-0008)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from studentassistant.config import DEFAULT_STT_LANGUAGE
from studentassistant.stt.models import AudioChunk, NormalisedSegment

ProviderState = Literal["idle", "streaming", "reconnecting", "unavailable"]
"""`idle`: nothing fed yet (or between streams); `streaming`: transcribing; `reconnecting`: lost
its service and retrying, audio meanwhile dropped; `unavailable`: cannot work at all."""

DEGRADED_STATES: frozenset[ProviderState] = frozenset({"reconnecting", "unavailable"})


@dataclass(frozen=True)
class ProviderStatus:
    """How a provider is doing; `detail` is a Spanish sentence for the student when not fine."""

    state: ProviderState
    detail: str | None = None

    @property
    def degraded(self) -> bool:
        """True while the provider is not transcribing (`reconnecting` or `unavailable`)."""
        return self.state in DEGRADED_STATES


IDLE = ProviderStatus("idle")


class SpeechToTextProvider(ABC):
    """Fed the session's audio chunk by chunk, it yields `NormalisedSegment`s in session time.

    Lifecycle: construct (from the provider's options table), `feed` every chunk in order, then
    `finish` once to flush what is still buffered; the provider is not fed after `finish`.
    Heavy work (model inference) must run off the event loop, e.g. `asyncio.to_thread`.

    Vocabulary hints (#54): `set_vocabulary` may be called at any time, before the first chunk and
    between chunks, with the session's domain terms (`stt.vocabulary.vocabulary_hints`); the
    latest list replaces the previous one and is in `vocabulary`. A provider that can bias its
    recognition (a Whisper prompt, cloud phrase hints) applies them from its next inference on;
    one that cannot simply ignores them (the default).

    Status (#222): `status` says whether the provider is transcribing; one that can fail at run
    time (a cloud API) reports `reconnecting` / `unavailable` there, with a Spanish `detail`,
    instead of raising into the session. It must be cheap and thread-safe to read: the gateway
    reads it after every chunk and tells the capture client when it changes. The default is
    always `idle` (a provider that never degrades).
    """

    # The name `stt.provider` selects it by, and the one its segments carry.
    name: ClassVar[str]

    def __init__(
        self, options: Mapping[str, Any] | None = None, *, language: str = DEFAULT_STT_LANGUAGE
    ) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self.language = language
        self.vocabulary: tuple[str, ...] = ()

    def set_vocabulary(self, hints: Sequence[str]) -> None:
        """Replace the session's vocabulary hints (most important first); never blocks."""
        self.vocabulary = tuple(hints)

    @property
    def status(self) -> ProviderStatus:
        """The provider's current state; never blocks."""
        return IDLE

    @abstractmethod
    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        """Take the next chunk; return the segments (partial or final) it made recognisable."""

    @abstractmethod
    async def finish(self) -> list[NormalisedSegment]:
        """No more audio: return every segment still pending."""
