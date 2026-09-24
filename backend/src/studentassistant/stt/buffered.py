"""Server mode without stalls: `BufferedProvider` decouples the gateway from model inference.

The WebSocket gateway calls `feed` for every audio frame and waits for it. A real provider
(faster-whisper, a cloud API) may take longer than the audio lasts, so `BufferedProvider` wraps
it: `feed` only enqueues the chunk and returns whatever the wrapped provider has produced since the
previous call, while one background task feeds the wrapped provider in order. Under a backlog
larger than `max_backlog_seconds` of queued audio, a buffered partial that a newer segment
supersedes is dropped (a stale hypothesis nobody needs any more); a final is never dropped.
`finish` waits for the queue to drain, flushes the wrapped provider and returns every remaining
segment.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import ClassVar

from studentassistant.config import DEFAULT_STT_MAX_BACKLOG_SECONDS, SttSettings
from studentassistant.stt.models import AudioChunk, NormalisedSegment
from studentassistant.stt.provider import SpeechToTextProvider
from studentassistant.stt.registry import provider_from_settings

logger = logging.getLogger(__name__)


class BufferedProvider(SpeechToTextProvider):
    """A `SpeechToTextProvider` whose `feed` never waits for the wrapped provider's inference.

    Its segments are the wrapped provider's own (they keep its `provider` name); `language` and
    `options` are the wrapped provider's too. `dropped` counts the partials dropped under backlog.
    """

    name: ClassVar[str] = "buffered"

    def __init__(
        self,
        inner: SpeechToTextProvider,
        *,
        max_backlog_seconds: float = DEFAULT_STT_MAX_BACKLOG_SECONDS,
    ) -> None:
        super().__init__(inner.options, language=inner.language)
        if max_backlog_seconds <= 0:
            raise ValueError("max_backlog_seconds must be positive")
        self.inner = inner
        self.max_backlog_seconds = max_backlog_seconds
        self.dropped = 0
        self._queue: deque[AudioChunk] = deque()
        # Seconds of audio fed to `feed` and not yet through the wrapped provider (queued or busy).
        self._backlog = 0.0
        self._ready: list[NormalisedSegment] = []
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._finished = False

    @property
    def backlog_seconds(self) -> float:
        """Seconds of audio waiting for (or inside) the wrapped provider."""
        return self._backlog

    async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
        """Queue `chunk`; return the segments produced since the previous call (never waits)."""
        if self._finished:
            raise RuntimeError("BufferedProvider was fed after finish()")
        self._queue.append(chunk)
        self._backlog += chunk.duration
        self._idle.clear()
        self._wakeup.set()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name=f"stt-buffer:{self.inner.name}")
        # Let the worker take the chunk now if the wrapped provider is free; never wait for it.
        await asyncio.sleep(0)
        return self._take()

    async def finish(self) -> list[NormalisedSegment]:
        """Wait for every queued chunk to be fed, flush the wrapped provider, return the rest."""
        if self._finished:
            return self._take()
        self._finished = True
        await self._idle.wait()
        self.close()
        try:
            tail = await self.inner.finish()
        except Exception:
            logger.exception("STT provider %r failed to finish", self.inner.name)
            tail = []
        return self._take() + tail

    def close(self) -> None:
        """Stop the background task (without flushing); `finish` calls it."""
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def _take(self) -> list[NormalisedSegment]:
        ready, self._ready = self._ready, []
        return ready

    def _offer(self, segments: list[NormalisedSegment]) -> None:
        if not segments:
            return
        self._ready.extend(segments)
        if self._backlog > self.max_backlog_seconds:
            last = len(self._ready) - 1
            kept = [s for i, s in enumerate(self._ready) if s.is_final or i == last]
            dropped = len(self._ready) - len(kept)
            if dropped:
                self.dropped += dropped
                logger.debug(
                    "STT backlog %.1f s > %.1f s: dropped %d superseded partials",
                    self._backlog,
                    self.max_backlog_seconds,
                    dropped,
                )
            self._ready = kept

    async def _run(self) -> None:
        while True:
            if not self._queue:
                self._idle.set()
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            chunk = self._queue.popleft()
            try:
                segments = await self.inner.feed(chunk)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("STT provider %r failed on a chunk; skipped", self.inner.name)
                segments = []
            finally:
                self._backlog = max(0.0, self._backlog - chunk.duration)
            self._offer(segments)


def buffered_provider_from_settings(settings: SttSettings) -> BufferedProvider:
    """The configured server-side provider (`provider_from_settings`), wrapped in a
    `BufferedProvider` bounded by `settings.max_backlog_seconds`; only valid in `server` mode."""
    return BufferedProvider(
        provider_from_settings(settings), max_backlog_seconds=settings.max_backlog_seconds
    )


__all__ = ["BufferedProvider", "buffered_provider_from_settings"]
