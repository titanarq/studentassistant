"""`TranscriptPipeline` on a real `SessionBus` and `tmp_vault`: bus finals -> `transcript.jsonl`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import BusEvent, SessionBus
from studentassistant.stt import TranscriptPipeline
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    end_session,
    start_session,
)

pytestmark = pytest.mark.anyio

# Built from pieces so this source never holds a string that looks like a real key.
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "Xy7Kq2Lm9Np4Rs8Tv1Wz" * 2


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
async def pipeline(bus: SessionBus, session: Session) -> AsyncIterator[TranscriptPipeline]:
    # Appends happen "5 s into the session" for the latency log.
    pipeline = TranscriptPipeline(
        bus, bus.attached, clock=lambda: session.meta.started_at + timedelta(seconds=5)
    )
    pipeline.start()
    yield pipeline
    await pipeline.stop()


def final(start_ms: int, end_ms: int, text: str, n: int, **extra: Any) -> dict[str, Any]:
    return {
        "segment_id": f"s{n}",
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "text": text,
        "language": "es",
        "provider": "web-speech",
        **extra,
    }


async def publish_final(bus: SessionBus, session: Session, payload: dict[str, Any]) -> None:
    await bus.publish(session.id, "transcript.final", "stt", payload, t=payload["session_start_ms"])


def transcript(session: Session) -> list[tuple[int, int, str]]:
    return [(s.t_start, s.t_end, s.text) for s in session.read_transcript()]


async def test_finals_are_appended_once_and_in_order(
    bus: SessionBus, session: Session, pipeline: TranscriptPipeline
) -> None:
    await publish_final(bus, session, final(0, 1_000, "la velocidad", 1))
    await publish_final(bus, session, final(0, 1_000, "la velocidad", 2))
    await publish_final(bus, session, final(1_200, 2_500, "es constante", 3, confidence=0.9))
    await pipeline.drain()

    assert transcript(session) == [(0, 1_000, "la velocidad"), (1_200, 2_500, "es constante")]
    assert [s.seq for s in session.read_transcript()] == [1, 2]


async def test_partials_are_ignored(
    bus: SessionBus, session: Session, pipeline: TranscriptPipeline
) -> None:
    await bus.publish(
        session.id, "transcript.partial", "stt", final(0, 500, "la", 1), persist=False, t=0
    )
    await publish_final(bus, session, final(0, 900, "la aceleración", 1))
    await pipeline.drain()

    assert transcript(session) == [(0, 900, "la aceleración")]


async def test_a_restarted_recognizer_overlap_is_merged(
    bus: SessionBus, session: Session, pipeline: TranscriptPipeline
) -> None:
    await publish_final(bus, session, final(0, 3_000, "la aceleración es constante", 1))
    await publish_final(bus, session, final(2_000, 5_000, "es constante en este caso", 2))
    await pipeline.drain()

    assert transcript(session) == [
        (0, 3_000, "la aceleración es constante"),
        (3_000, 5_000, "en este caso"),
    ]


async def test_every_final_before_session_ended_is_written_and_none_after(
    bus: SessionBus,
    session: Session,
    pipeline: TranscriptPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for n in range(20):
        await publish_final(bus, session, final(n * 1_000, n * 1_000 + 800, f"frase {n}", n))
    await bus.publish(session.id, "session.ended", "user", {"reason": "button"})
    await pipeline.drain()
    end_session(session)
    bus.detach(session.id)

    assert [text for _, _, text in transcript(session)] == [f"frase {n}" for n in range(20)]

    # A final that reaches the pipeline after the end is logged, never written nor raised.
    with caplog.at_level(logging.WARNING, logger="studentassistant.stt.pipeline"):
        bus_offer(bus, bus_event(session, final(30_000, 31_000, "x", 99)))
        await pipeline.drain()
    assert len(transcript(session)) == 20
    assert "after its end" in caplog.text
    assert pipeline.running


async def test_an_append_to_an_ended_session_is_logged_not_raised(
    bus: SessionBus,
    session: Session,
    pipeline: TranscriptPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await publish_final(bus, session, final(0, 500, "antes", 1))
    await pipeline.drain()
    end_session(session)  # the vault ended it before the pipeline saw `session.ended`

    with caplog.at_level(logging.ERROR, logger="studentassistant.stt.pipeline"):
        # The bus itself refuses to log it once the session ended: deliver it directly.
        bus_offer(bus, bus_event(session, final(1_000, 1_500, "después", 2)))
        await pipeline.drain()

    assert transcript(session) == [(0, 500, "antes")]
    assert "is lost" in caplog.text
    assert pipeline.running


async def test_a_secret_looking_final_is_skipped_and_the_pipeline_goes_on(
    bus: SessionBus,
    session: Session,
    pipeline: TranscriptPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = final(0, 500, f"la clave es {ANTHROPIC_KEY}", 1)
    with caplog.at_level(logging.WARNING, logger="studentassistant.stt.pipeline"):
        # The bus refuses to log it too: deliver it directly.
        bus_offer(bus, bus_event(session, secret))
        await publish_final(bus, session, final(1_000, 2_000, "sin secretos", 2))
        await pipeline.drain()

    assert transcript(session) == [(1_000, 2_000, "sin secretos")]
    assert "looks like a secret" in caplog.text
    assert ANTHROPIC_KEY not in caplog.text


async def test_latency_is_logged_per_segment(
    bus: SessionBus,
    session: Session,
    pipeline: TranscriptPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="studentassistant.stt.pipeline"):
        await publish_final(bus, session, final(3_000, 4_200, "la velocidad", 1))
        await pipeline.drain()

    records = [r for r in caplog.records if hasattr(r, "latency_ms")]
    assert len(records) == 1
    record = records[0]
    assert record.name == "studentassistant.stt.pipeline"
    assert record.latency_ms == 800  # appended at 5 000 ms, segment ended at 4 200 ms
    assert record.session_id == session.id
    assert record.provider == "web-speech"
    assert "latency 800 ms" in record.getMessage()


async def test_stop_writes_what_was_already_delivered(bus: SessionBus, session: Session) -> None:
    pipeline = TranscriptPipeline(bus, bus.attached)
    pipeline.start()
    await publish_final(bus, session, final(0, 500, "uno", 1))
    await publish_final(bus, session, final(600, 900, "dos", 2))
    await pipeline.stop()

    assert [text for _, _, text in transcript(session)] == ["uno", "dos"]
    assert not pipeline.running


async def test_a_resumed_session_is_not_written_twice(bus: SessionBus, session: Session) -> None:
    session.append_transcript(0, 1_000, "la velocidad")
    pipeline = TranscriptPipeline(bus, bus.attached)
    pipeline.start()
    await publish_final(bus, session, final(0, 1_000, "la velocidad", 1))
    await publish_final(bus, session, final(500, 2_000, "velocidad es constante", 2))
    await pipeline.stop()

    assert transcript(session) == [(0, 1_000, "la velocidad"), (1_000, 2_000, "es constante")]


# -- helpers for events the bus itself would refuse to log -------------------------------------


def bus_event(session: Session, payload: dict[str, Any]) -> BusEvent:
    """A `transcript.final` BusEvent as the bus would deliver it, without logging it."""
    return BusEvent(
        session_id=session.id,
        subject_id=session.subject_slug,
        topic_id=session.topic_slug,
        kind="transcript.final",
        origin="stt",
        t=payload["session_start_ms"],
        payload=payload,
        seq=1_000,
    )


def bus_offer(bus: SessionBus, event: BusEvent) -> None:
    for subscription in bus.subscriptions:
        if subscription.wants(event):
            subscription.offer(event)
