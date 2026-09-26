"""`CommandDetector` on a real `SessionBus` and `tmp_vault`: transcript events -> `voice.command`
and `command` `capture_now` in `events.jsonl`, each exactly once."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.stt import CommandDetector, load_grammar
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    start_session,
)

pytestmark = pytest.mark.anyio

PROTOCOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault) -> Session:
    subject = create_subject(tmp_vault, "Física")
    topic = create_topic(tmp_vault, subject.slug, "Cinemática")
    return start_session(tmp_vault, subject.slug, topic.slug, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
async def detector(bus: SessionBus) -> AsyncIterator[CommandDetector]:
    detector = CommandDetector(bus, load_grammar())
    detector.start()
    yield detector
    await detector.stop()


def segment(segment_id: str, text: str, start_ms: int = 1_500) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "session_start_ms": start_ms,
        "session_end_ms": start_ms + 800,
        "text": text,
        "language": "es",
        "provider": "web-speech",
    }


async def partial(bus: SessionBus, session: Session, payload: dict[str, Any]) -> None:
    await bus.publish(
        session.id,
        "transcript.partial",
        "stt",
        payload,
        t=payload["session_start_ms"],
        persist=False,
    )


async def final(bus: SessionBus, session: Session, payload: dict[str, Any]) -> None:
    await bus.publish(session.id, "transcript.final", "stt", payload, t=payload["session_start_ms"])


def logged(session: Session, *kinds: str) -> list[tuple[str, str, int, dict[str, Any]]]:
    return [
        (e.kind, e.origin, e.t, dict(e.payload)) for e in session.read_events() if e.kind in kinds
    ]


async def test_growing_capture_utterance_fires_once_with_a_capture_now(
    bus: SessionBus, session: Session, detector: CommandDetector
) -> None:
    await partial(bus, session, segment("s1", "mira"))
    await partial(bus, session, segment("s1", "mira aquí"))
    await partial(bus, session, segment("s1", "mira aquí"))
    await final(bus, session, segment("s1", "Mira aquí."))
    await detector.drain()

    events = logged(session, "voice.command", "command")
    assert [(kind, origin, t) for kind, origin, t, _ in events] == [
        ("voice.command", "stt", 1_500),
        ("command", "stt", events[1][2]),
    ]
    # "mira aquí" ending a partial could still grow into "mira, aquí no": it waits for the final.
    assert events[0][3] == {"command": "capture", "segment_id": "s1", "text": "Mira aquí."}
    command = events[1][3]
    assert command["command"] == "capture_now"
    assert command["voice_command"] == "capture"
    assert command["segment_id"] == "s1"
    assert PROTOCOL_ID.match(command["command_id"])


async def test_excluded_utterance_as_growing_partials_fires_nothing(
    bus: SessionBus, session: Session, detector: CommandDetector
) -> None:
    await partial(bus, session, segment("s1", "mira"))
    await partial(bus, session, segment("s1", "mira aquí"))
    await partial(bus, session, segment("s1", "mira, aquí no"))
    await final(bus, session, segment("s1", "Mira, aquí no."))
    await detector.drain()

    assert logged(session, "voice.command", "command") == []


async def test_payloads_of_source_query_and_plain_commands(
    bus: SessionBus, session: Session, detector: CommandDetector
) -> None:
    await final(bus, session, segment("s1", "Ahora el libro", start_ms=100))
    await partial(bus, session, segment("s2", "busca en internet", start_ms=200))
    await final(bus, session, segment("s2", "Busca en internet: la ley de Ohm", start_ms=200))
    await final(bus, session, segment("s3", "esto es importante", start_ms=300))
    await detector.drain()

    assert logged(session, "voice.command") == [
        (
            "voice.command",
            "stt",
            100,
            {
                "command": "source_book",
                "segment_id": "s1",
                "text": "Ahora el libro",
                "source": "book",
            },
        ),
        (
            "voice.command",
            "stt",
            200,
            {
                "command": "web_search",
                "segment_id": "s2",
                "text": "Busca en internet: la ley de Ohm",
                "query": "la ley de Ohm",
            },
        ),
        (
            "voice.command",
            "stt",
            300,
            {"command": "important", "segment_id": "s3", "text": "esto es importante"},
        ),
    ]
    assert logged(session, "command") == []


async def test_capture_now_ids_are_unique_per_session(
    bus: SessionBus, session: Session, detector: CommandDetector
) -> None:
    await final(bus, session, segment("s1", "captura"))
    await final(bus, session, segment("s2", "siguiente"))
    await final(bus, session, segment("s3", "haz foto"))
    await detector.drain()

    commands = [p for _, _, _, p in logged(session, "command")]
    assert [p["voice_command"] for p in commands] == ["capture", "next_page", "capture"]
    ids = [p["command_id"] for p in commands]
    assert len(set(ids)) == 3
    assert all(PROTOCOL_ID.match(i) for i in ids)


async def test_session_ended_drops_the_debounce_state(
    bus: SessionBus, session: Session, detector: CommandDetector
) -> None:
    await final(bus, session, segment("s1", "importante"))
    await detector.drain()
    assert detector._matchers.keys() == {session.id}

    await bus.publish(session.id, "session.ended", "user", {})
    await detector.drain()
    assert detector._matchers == {}

    # The same segment id in a later run of the session fires again (fresh state).
    await final(bus, session, segment("s1", "importante"))
    await detector.drain()
    assert [p["command"] for _, _, _, p in logged(session, "voice.command")] == [
        "important",
        "important",
    ]


async def test_a_failing_event_is_logged_and_the_detector_carries_on(
    bus: SessionBus,
    session: Session,
    detector: CommandDetector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await final(bus, session, {"segment_id": "s1", "session_start_ms": 0})
    await detector.drain()
    assert "malformed" in caplog.text

    real_publish = bus.publish
    calls = 0

    async def failing_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if args[1] == "voice.command":
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
        return await real_publish(*args, **kwargs)

    bus.publish = failing_once  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="studentassistant.stt.commands"):
        await final(bus, session, segment("s2", "pausa"))
        await detector.drain()
        await final(bus, session, segment("s3", "reanuda"))
        await detector.drain()

    assert "voice-command detector failed" in caplog.text
    assert detector.running
    assert [p["command"] for _, _, _, p in logged(session, "voice.command")] == ["resume"]
