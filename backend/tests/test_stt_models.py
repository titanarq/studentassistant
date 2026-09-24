"""STT segment models: field names, defaults and span/confidence validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from studentassistant.stt import AudioChunk, ClientSegment, NormalisedSegment


def test_normalised_segment_fields_and_defaults() -> None:
    segment = NormalisedSegment(start=1.5, end=3.0, text="la derivada", provider="web-speech")

    assert set(NormalisedSegment.model_fields) == {
        "start",
        "end",
        "text",
        "provider",
        "confidence",
        "is_final",
    }
    assert segment.confidence is None
    assert segment.is_final is True


def test_client_segment_fields_and_defaults() -> None:
    segment = ClientSegment(client_start=100.0, client_end=101.2, text="hola")

    assert set(ClientSegment.model_fields) == {
        "client_start",
        "client_end",
        "text",
        "confidence",
        "is_final",
    }
    assert segment.confidence is None
    assert segment.is_final is True


def test_an_instant_segment_is_valid() -> None:
    assert NormalisedSegment(start=2.0, end=2.0, text="", provider="fake").end == 2.0


def test_inverted_span_is_rejected() -> None:
    with pytest.raises(ValidationError, match="before it starts"):
        NormalisedSegment(start=3.0, end=1.0, text="x", provider="fake")
    with pytest.raises(ValidationError, match="before it starts"):
        ClientSegment(client_start=5.0, client_end=4.0, text="x")


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_out_of_range_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        NormalisedSegment(start=0, end=1, text="x", provider="fake", confidence=confidence)
    with pytest.raises(ValidationError):
        ClientSegment(client_start=0, client_end=1, text="x", confidence=confidence)


def test_audio_chunk_duration_follows_its_pcm_format() -> None:
    chunk = AudioChunk(start=2.0, data=b"\x00" * 32_000)  # 1 s of 16 kHz 16-bit mono

    assert chunk.duration == pytest.approx(1.0)
    assert chunk.end == pytest.approx(3.0)
