"""Pluggable STT: segment ingestion, providers (Whisper, cloud), voice-command grammar."""

from studentassistant.stt.buffered import BufferedProvider, buffered_provider_from_settings
from studentassistant.stt.commands import (
    CommandDetector,
    CommandGrammar,
    CommandMatcher,
    CommandSpec,
    FiredCommand,
    GrammarError,
    load_grammar,
)
from studentassistant.stt.models import AudioChunk, ClientSegment, NormalisedSegment
from studentassistant.stt.pipeline import EventBus, SessionLookup, TranscriptPipeline
from studentassistant.stt.provider import (
    DEGRADED_STATES,
    ProviderState,
    ProviderStatus,
    SpeechToTextProvider,
)
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
from studentassistant.stt.vocabulary import (
    hotwords_text,
    vocabulary_hints,
    vocabulary_hints_from_settings,
)

__all__ = [
    "DEGRADED_STATES",
    "ENTRY_POINT_GROUP",
    "AudioChunk",
    "BufferedProvider",
    "ClientSegment",
    "CommandDetector",
    "CommandGrammar",
    "CommandMatcher",
    "CommandSpec",
    "EventBus",
    "FiredCommand",
    "GrammarError",
    "InMemoryTranscriptSink",
    "NormalisedSegment",
    "NotServerModeError",
    "ProviderState",
    "ProviderStatus",
    "SessionLookup",
    "SpeechToTextProvider",
    "TranscriptAssembler",
    "TranscriptPipeline",
    "TranscriptSink",
    "UnknownProviderError",
    "buffered_provider_from_settings",
    "get_provider",
    "hotwords_text",
    "load_grammar",
    "provider_class",
    "provider_from_settings",
    "register_provider",
    "registered_providers",
    "unregister_provider",
    "vocabulary_hints",
    "vocabulary_hints_from_settings",
]
