"""Pluggable STT: segment ingestion, providers (Whisper, cloud), voice-command grammar."""

from studentassistant.stt.buffered import BufferedProvider, buffered_provider_from_settings
from studentassistant.stt.models import AudioChunk, ClientSegment, NormalisedSegment
from studentassistant.stt.pipeline import EventBus, SessionLookup, TranscriptPipeline
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
from studentassistant.stt.transcript import TranscriptAssembler

__all__ = [
    "ENTRY_POINT_GROUP",
    "AudioChunk",
    "BufferedProvider",
    "ClientSegment",
    "EventBus",
    "InMemoryTranscriptSink",
    "NormalisedSegment",
    "NotServerModeError",
    "SessionLookup",
    "SpeechToTextProvider",
    "TranscriptAssembler",
    "TranscriptPipeline",
    "TranscriptSink",
    "UnknownProviderError",
    "buffered_provider_from_settings",
    "get_provider",
    "provider_class",
    "provider_from_settings",
    "register_provider",
    "registered_providers",
    "unregister_provider",
]
