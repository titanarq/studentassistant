"""The session event bus: ordering, seq continuity, slow subscribers, never-dropped events."""

from __future__ import annotations

import asyncio

import pytest

from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import BusEvent, SessionBus, SessionNotAttachedError
from studentassistant.vault import (
    SecretRefused,
    Session,
    Vault,
    create_subject,
    create_topic,
    resume_session,
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


async def test_persisted_events_get_consecutive_seq_and_land_in_the_log(
    bus: SessionBus, session: Session
) -> None:
    with bus.subscribe(name="test") as subscription:
        published = [
            await bus.publish(session.id, "marker", "phone", {"n": n}) for n in range(1, 6)
        ]
        received = [subscription.get_nowait() for _ in range(5)]

    assert [event.seq for event in published] == [1, 2, 3, 4, 5]
    assert received == published
    logged = list(session.read_events())
    assert [(e.seq, e.kind, e.origin, e.payload) for e in logged] == [
        (n, "marker", "phone", {"n": n}) for n in range(1, 6)
    ]
    assert received[0].subject_id == session.subject_slug
    assert received[0].topic_id == session.topic_slug


async def test_notices_are_delivered_in_order_but_never_written(
    bus: SessionBus, session: Session
) -> None:
    with bus.subscribe() as subscription:
        await bus.publish(session.id, "marker", "phone")
        notice = await bus.publish(
            session.id, "transcript.partial", "stt", {"text": "hola"}, persist=False
        )
        await bus.publish(session.id, "marker", "phone")
        kinds = [subscription.get_nowait() for _ in range(3)]

    assert notice.seq is None and not notice.persisted
    assert [(e.kind, e.seq) for e in kinds] == [
        ("marker", 1),
        ("transcript.partial", None),
        ("marker", 2),
    ]
    assert [e.seq for e in session.read_events()] == [1, 2]


async def test_concurrent_publishers_are_delivered_in_seq_order(
    bus: SessionBus, session: Session
) -> None:
    with bus.subscribe() as subscription:
        await asyncio.gather(
            *(bus.publish(session.id, "marker", "phone", {"n": n}) for n in range(20))
        )
        seqs = [subscription.get_nowait().seq for _ in range(20)]
    assert seqs == list(range(1, 21))


async def test_seq_continues_after_the_session_is_resumed(
    tmp_vault: Vault, bus: SessionBus, session: Session
) -> None:
    for _ in range(3):
        await bus.publish(session.id, "marker", "phone")
    bus.detach(session.id)

    resumed = resume_session(tmp_vault, session.subject_slug, session.topic_slug)
    fresh = SessionBus()
    fresh.attach(resumed)
    event = await fresh.publish(resumed.id, "marker", "phone")

    assert event.seq == 4
    assert [e.seq for e in resumed.read_events()] == [1, 2, 3, 4]


async def test_a_slow_subscriber_never_blocks_the_publisher(
    bus: SessionBus, session: Session
) -> None:
    slow = bus.subscribe(name="slow", maxsize=2)  # never read during the publishes
    for n in range(10):
        await asyncio.wait_for(
            bus.publish(session.id, "transcript.partial", "stt", {"n": n}, persist=False), 1
        )
    await asyncio.wait_for(bus.publish(session.id, "marker", "phone"), 1)

    # Drop-oldest for notices: the newest notice and the persisted event are what is left.
    left = [slow.get_nowait() for _ in range(len(slow))]
    assert [(e.kind, e.payload.get("n")) for e in left] == [
        ("transcript.partial", 9),
        ("marker", None),
    ]
    assert slow.dropped == 9
    slow.close()


async def test_persisted_events_are_never_dropped_even_when_the_queue_is_full(
    bus: SessionBus, session: Session
) -> None:
    slow = bus.subscribe(name="slow", maxsize=3)
    await bus.publish(session.id, "transcript.partial", "stt", persist=False)
    for _ in range(10):
        await bus.publish(session.id, "marker", "phone")
    await bus.publish(session.id, "transcript.partial", "stt", persist=False)

    left = [slow.get_nowait() for _ in range(len(slow))]
    assert [e.seq for e in left] == list(range(1, 11))  # both notices gone, no event lost
    assert slow.dropped == 2
    assert slow.overflowed == 7  # e3 took the first notice's slot
    slow.close()


async def test_filters_by_session_and_kind(bus: SessionBus, session: Session) -> None:
    only_markers = bus.subscribe(kinds={"marker"})
    other_session = bus.subscribe(session_id="20000101-000000")
    await bus.publish(session.id, "button", "phone")
    await bus.publish(session.id, "marker", "phone")

    assert [e.kind for e in (only_markers.get_nowait(),)] == ["marker"]
    assert len(only_markers) == 0
    assert len(other_session) == 0


async def test_a_waiting_reader_is_woken_and_iteration_ends_on_close(
    bus: SessionBus, session: Session
) -> None:
    subscription = bus.subscribe()
    received: list[BusEvent] = []

    async def read() -> None:
        async for event in subscription:
            received.append(event)

    reader = asyncio.create_task(read())
    await asyncio.sleep(0)
    await bus.publish(session.id, "marker", "phone")
    await asyncio.sleep(0)
    subscription.close()
    await asyncio.wait_for(reader, 1)

    assert [e.seq for e in received] == [1]
    assert subscription not in bus.subscriptions


async def test_publishing_for_a_session_not_attached_is_refused(
    bus: SessionBus, session: Session
) -> None:
    with pytest.raises(SessionNotAttachedError):
        await bus.publish("20000101-000000", "marker", "phone")
    bus.detach(session.id)
    with pytest.raises(SessionNotAttachedError):
        await bus.publish(session.id, "marker", "phone")


async def test_a_refused_append_delivers_nothing_and_keeps_the_seq(
    bus: SessionBus, session: Session
) -> None:
    with bus.subscribe() as subscription:
        with pytest.raises(SecretRefused):
            await bus.publish(session.id, "marker", "phone", {"text": ANTHROPIC_KEY})
        assert len(subscription) == 0
        event = await bus.publish(session.id, "marker", "phone")
    assert event.seq == 1


async def test_on_append_is_called_for_each_persisted_event(session: Session) -> None:
    calls: list[None] = []
    bus = SessionBus(on_append=lambda: calls.append(None))
    bus.attach(session)
    await bus.publish(session.id, "marker", "phone")
    await bus.publish(session.id, "transcript.partial", "stt", persist=False)
    assert len(calls) == 1
