"""Provider registry: `fake` by entry point, driving it, unknown names, lazy imports."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from studentassistant.config import SttSettings
from studentassistant.stt import (
    ENTRY_POINT_GROUP,
    AudioChunk,
    NormalisedSegment,
    NotServerModeError,
    SpeechToTextProvider,
    UnknownProviderError,
    get_provider,
    provider_from_settings,
    register_provider,
    registered_providers,
    unregister_provider,
)

SCRIPT = [
    {"start": 0.2, "end": 0.9, "text": "hoy vemos", "is_final": False},
    {"start": 0.2, "end": 1.8, "text": "hoy vemos derivadas", "confidence": 0.9},
    {"start": 2.1, "end": 3.5, "text": "regla de la cadena", "confidence": 0.8},
]


def second_of_audio(start: float) -> AudioChunk:
    return AudioChunk(start=start, data=b"\x00" * 32_000)


async def drive(provider: SpeechToTextProvider, chunks: list[AudioChunk]) -> list[list[str]]:
    batches = [[s.text for s in await provider.feed(chunk)] for chunk in chunks]
    batches.append([s.text for s in await provider.finish()])
    return batches


def test_fake_resolves_by_name_through_its_entry_point() -> None:
    assert "fake" in registered_providers()

    provider = get_provider("fake", {"segments": SCRIPT}, language="es")

    assert provider.name == "fake"
    assert provider.language == "es"


def test_fake_yields_scripted_segments_as_the_audio_reaches_them() -> None:
    provider = get_provider("fake", {"segments": SCRIPT})

    batches = asyncio.run(drive(provider, [second_of_audio(0.0), second_of_audio(1.0)]))

    assert batches == [["hoy vemos"], ["hoy vemos derivadas"], ["regla de la cadena"]]


def test_fake_segments_are_normalised_segments() -> None:
    provider = get_provider("fake", {"segments": SCRIPT})

    segments = asyncio.run(provider.feed(AudioChunk(start=0.0, data=b"\x00" * 64_000)))

    assert segments == [
        NormalisedSegment(start=0.2, end=0.9, text="hoy vemos", provider="fake", is_final=False),
        NormalisedSegment(
            start=0.2, end=1.8, text="hoy vemos derivadas", provider="fake", confidence=0.9
        ),
    ]


def test_fake_refuses_audio_after_finish() -> None:
    provider = get_provider("fake")
    asyncio.run(provider.finish())

    with pytest.raises(RuntimeError):
        asyncio.run(provider.feed(second_of_audio(0.0)))


def test_unknown_provider_error_names_the_registered_ones() -> None:
    with pytest.raises(UnknownProviderError) as excinfo:
        get_provider("no-such-provider")

    message = str(excinfo.value)
    assert "'no-such-provider'" in message
    assert "registered providers: " in message
    assert "fake" in message


def test_a_provider_registered_in_code_resolves() -> None:
    class Silent(SpeechToTextProvider):
        name = "silent"

        async def feed(self, chunk: AudioChunk) -> list[NormalisedSegment]:
            return []

        async def finish(self) -> list[NormalisedSegment]:
            return []

    register_provider("silent", Silent)
    try:
        assert isinstance(get_provider("silent", {"x": 1}), Silent)
        assert get_provider("silent", {"x": 1}).options == {"x": 1}
        assert "silent" in registered_providers()
    finally:
        unregister_provider("silent")
    assert "silent" not in registered_providers()


def test_provider_from_settings_uses_the_configured_options() -> None:
    settings = SttSettings(mode="server", provider="fake", options={"fake": {"segments": SCRIPT}})

    provider = provider_from_settings(settings)

    assert provider.name == "fake"
    assert asyncio.run(provider.finish())[-1].text == "regla de la cadena"


def test_provider_from_settings_refuses_client_mode() -> None:
    with pytest.raises(NotServerModeError):
        provider_from_settings(SttSettings())


def test_importing_stt_pulls_in_no_concrete_provider() -> None:
    code = (
        "import sys, studentassistant.stt\n"
        "print('\\n'.join(m for m in sys.modules if m.startswith('studentassistant.stt')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()

    assert set(loaded) == {
        "studentassistant.stt",
        "studentassistant.stt.buffered",
        "studentassistant.stt.models",
        "studentassistant.stt.pipeline",
        "studentassistant.stt.provider",
        "studentassistant.stt.registry",
        "studentassistant.stt.sink",
        "studentassistant.stt.transcript",
    }
    assert ENTRY_POINT_GROUP == "studentassistant.stt_providers"
