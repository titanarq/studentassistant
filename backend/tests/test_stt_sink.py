"""`TranscriptSink`: client segments become the same `NormalisedSegment`s a provider yields."""

from __future__ import annotations

import asyncio

import pytest

from studentassistant.config import SttSettings
from studentassistant.stt import InMemoryTranscriptSink, NormalisedSegment, get_provider
from studentassistant.stt.fakes import ScriptedClientSource

SCRIPT = [
    {"start": 0.2, "end": 0.9, "text": "hoy vemos", "is_final": False},
    {"start": 0.2, "end": 1.8, "text": "hoy vemos derivadas", "confidence": 0.9},
]
CLIENT_CLOCK_AT_SESSION_START = 1000.0


def client_source() -> ScriptedClientSource:
    return ScriptedClientSource(
        [
            {
                "client_start": CLIENT_CLOCK_AT_SESSION_START + s["start"],
                "client_end": CLIENT_CLOCK_AT_SESSION_START + s["end"],
                **{k: v for k, v in s.items() if k not in ("start", "end")},
            }
            for s in SCRIPT
        ]
    )


def test_sink_normalises_client_segments_with_the_configured_provider() -> None:
    sink = InMemoryTranscriptSink.from_settings(
        SttSettings(provider="web-speech"), clock_offset=CLIENT_CLOCK_AT_SESSION_START
    )

    produced = asyncio.run(client_source().play_into(sink))

    assert produced == sink.segments
    assert [s.provider for s in produced] == ["web-speech", "web-speech"]
    assert produced[0].start == pytest.approx(0.2)
    assert produced[1].end == pytest.approx(1.8)


def test_sink_output_matches_what_fake_provider_yields() -> None:
    sink = InMemoryTranscriptSink("fake", clock_offset=CLIENT_CLOCK_AT_SESSION_START)
    from_client = asyncio.run(client_source().play_into(sink))
    from_server = asyncio.run(get_provider("fake", {"segments": SCRIPT}).finish())

    assert all(type(s) is NormalisedSegment for s in from_client + from_server)
    assert [set(s.model_dump()) for s in from_client] == [set(s.model_dump()) for s in from_server]
    for client, server in zip(from_client, from_server, strict=True):
        assert client.model_dump(exclude={"start", "end"}) == server.model_dump(
            exclude={"start", "end"}
        )
        assert client.start == pytest.approx(server.start)
        assert client.end == pytest.approx(server.end)
