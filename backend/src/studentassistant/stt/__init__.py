"""Pluggable STT: segment ingestion, providers (Whisper, cloud), voice-command grammar."""

from studentassistant.stt.models import AudioChunk, ClientSegment, NormalisedSegment
from studentassistant.stt.provider import SpeechToTextProvider
from studentassistant.stt.registry import (
    ENTRY_POINT_GROUP,
    NotServerModeError,
    UnknownProviderError,
    get_provider,
    provider_class,
    provider_from_settings,
    register_provider,
    registered_providers,
    unregister_provider,
)
from studentassistant.stt.sink import InMemoryTranscriptSink, TranscriptSink

__all__ = [
    "ENTRY_POINT_GROUP",
    "AudioChunk",
    "ClientSegment",
    "InMemoryTranscriptSink",
    "NormalisedSegment",
    "NotServerModeError",
    "SpeechToTextProvider",
    "TranscriptSink",
    "UnknownProviderError",
    "get_provider",
    "provider_class",
    "provider_from_settings",
    "register_provider",
    "registered_providers",
    "unregister_provider",
]
