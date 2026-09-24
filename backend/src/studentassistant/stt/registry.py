"""Server-side STT providers by name: registered in code or discovered through entry points.

A provider package declares, in its `pyproject.toml`,

    [project.entry-points."studentassistant.stt_providers"]
    faster-whisper = "studentassistant.stt.faster_whisper:FasterWhisperProvider"

and `stt.provider = "faster-whisper"` selects it. Entry points are loaded only when their name is
asked for, so importing `studentassistant.stt` never imports a concrete provider (nor its heavy
optional dependencies).
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from studentassistant.config import DEFAULT_STT_LANGUAGE, SttSettings
from studentassistant.stt.provider import SpeechToTextProvider

ENTRY_POINT_GROUP = "studentassistant.stt_providers"

_registered: dict[str, type[SpeechToTextProvider]] = {}


class UnknownProviderError(LookupError):
    """`stt.provider` names no registered server-side provider."""


class NotServerModeError(RuntimeError):
    """A server-side provider was asked for while `stt.mode` is `client`."""


def register_provider(name: str, provider: type[SpeechToTextProvider]) -> None:
    """Register `provider` under `name`, taking precedence over an entry point of that name."""
    _registered[name] = provider


def unregister_provider(name: str) -> None:
    """Forget a provider registered in code (entry points stay discoverable)."""
    _registered.pop(name, None)


def _entry_points() -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def registered_providers() -> list[str]:
    """Every provider name `get_provider` resolves, sorted."""
    return sorted(set(_registered) | set(_entry_points()))


def provider_class(name: str) -> type[SpeechToTextProvider]:
    """The provider class registered under `name`; loads its entry point on first use."""
    if name in _registered:
        return _registered[name]
    ep = _entry_points().get(name)
    if ep is None:
        known = ", ".join(registered_providers()) or "none"
        raise UnknownProviderError(f"unknown STT provider {name!r}; registered providers: {known}")
    loaded = ep.load()
    if not (isinstance(loaded, type) and issubclass(loaded, SpeechToTextProvider)):
        raise TypeError(f"entry point {ep.value!r} is not a SpeechToTextProvider subclass")
    _registered[name] = loaded
    return loaded


def get_provider(
    name: str,
    options: Mapping[str, Any] | None = None,
    *,
    language: str = DEFAULT_STT_LANGUAGE,
) -> SpeechToTextProvider:
    """A new instance of the provider registered under `name`, built from its options table."""
    return provider_class(name)(options, language=language)


def provider_from_settings(settings: SttSettings) -> SpeechToTextProvider:
    """The server-side provider the configuration selects; only valid in `server` mode."""
    if settings.mode != "server":
        raise NotServerModeError(
            f"stt.mode is {settings.mode!r}: segments come from the client "
            f"({settings.provider!r}) through a TranscriptSink, not from a server-side provider"
        )
    return get_provider(settings.provider, settings.provider_options(), language=settings.language)
