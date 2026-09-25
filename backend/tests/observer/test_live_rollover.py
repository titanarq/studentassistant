"""Context purge (#60): the observer conversation rolls over to snapshot + digest + tail.

`ObserverLoop` on a real `SessionBus` and `tmp_vault`, with `FakeClaude` as the observer role and
scripted usage to cross the token threshold. Every wait is bounded (`asyncio.wait_for`), so a loop
that never settles fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from studentassistant.config import LlmSettings, ObserverSettings, Settings
from studentassistant.llm import FakeClaude, LLMRequest, Usage
from studentassistant.observer import load_observer_snapshot
from studentassistant.observer.catchup import ACK_EVENT_KIND, unanswered
from studentassistant.observer.live import (
    CONTEXT_ROLLED_EVENT_KIND,
    TOOL_NAME,
    ObserverLoop,
    default_client_factory,
)
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    read_conversation,
    read_topic_events,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 5.0
DIGEST = "# La célula\n\nResumen de las sesiones anteriores."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def session(tmp_vault: Vault) -> Session:
    subject = create_subject(tmp_vault, "Biología")
    topic = create_topic(tmp_vault, subject.slug, "La célula")
    return start_session(tmp_vault, subject.slug, topic.slug, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
async def loop(bus: SessionBus, fake: FakeClaude) -> AsyncIterator[ObserverLoop]:
    settings = Settings(
        llm=LlmSettings(),
        observer=ObserverSettings(
            batch_segments=2,
            batch_speech_seconds=1000,
            context_max_tokens=1500,
            context_tail_segments=2,
        ),
    )
    observer = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
        digest=lambda _vault, _subject, _topic: DIGEST,
    )
    observer.start()
    yield observer
    await asyncio.wait_for(observer.stop(), WAIT)


async def segment(bus: SessionBus, session: Session, n: int, text: str) -> None:
    start = n * 10_000
    await bus.publish(
        session.id,
        "transcript.final",
        "stt",
        {
            "segment_id": f"seg-{n}",
            "session_start_ms": start,
            "session_end_ms": start + 2000,
            "text": text,
            "provider": "fake",
        },
        t=start,
    )


async def batch(loop: ObserverLoop, bus: SessionBus, session: Session, first: int) -> None:
    await segment(bus, session, first, f"Frase número {first}.")
    await segment(bus, session, first + 1, f"Frase número {first + 1}.")
    await asyncio.wait_for(loop.wait_idle(session.id), WAIT)


def reply(fake: FakeClaude, *ops: dict[str, Any], usage: Usage) -> None:
    fake.reply_tool(TOOL_NAME, {"ops": list(ops)}, usage=usage)


def rolled_events(session: Session) -> list[dict[str, Any]]:
    return [
        dict(event.payload)
        for event in session.read_events()
        if event.kind == CONTEXT_ROLLED_EVENT_KIND
    ]


def texts(request: LLMRequest) -> str:
    return json.dumps(request.messages, ensure_ascii=False)


async def test_past_the_threshold_the_next_call_starts_from_state_digest_and_tail(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude, tmp_vault: Vault
) -> None:
    reply(
        fake,
        {"op": "add_section", "section_id": "sec-1", "title": "Membrana"},
        {"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-1", "seg-2"]},
        usage=Usage(input_tokens=1000, output_tokens=100),
    )
    # 1400 prompt tokens (mostly cached) + 200 of answer: 1600 >= 1500, the context rolls over.
    reply(
        fake,
        {"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-3", "seg-4"]},
        usage=Usage(input_tokens=100, cache_read_input_tokens=1300, output_tokens=200),
    )
    reply(fake, usage=Usage(input_tokens=300, cache_read_input_tokens=500, output_tokens=50))
    reply(fake, usage=Usage(input_tokens=100, cache_read_input_tokens=900, output_tokens=50))

    await batch(loop, bus, session, 1)
    await batch(loop, bus, session, 3)
    assert len(fake.requests[1].messages) == 3  # still one growing conversation
    assert rolled_events(session) == []  # recorded once the new conversation is answered

    await batch(loop, bus, session, 5)
    first, third = fake.requests[0], fake.requests[2]
    # The new conversation: the same cached prefix (prompt + topic block with the digest, tool)...
    assert third.system == first.system
    assert "Resumen de las sesiones anteriores." in third.system[-1]["text"]
    assert third.tools == first.tools
    # ...and one user turn: the state so far, the tail, the batch. Nothing else.
    assert len(third.messages) == 1
    content = third.messages[0]["content"]
    assert [block["type"] for block in content] == ["text", "text"]
    state, turn = content[0]["text"], content[1]["text"]
    assert "continues" in state and "sec-1: Membrana (4)" in state
    tail = state.split("already answered")[1]
    assert "Frase número 3." in tail and "Frase número 4." in tail
    assert "Frase número 1." not in texts(third) and "Frase número 2." not in texts(third)
    assert "tool_result" not in texts(third)
    assert "segment seg-5" in turn and "segment seg-6" in turn and "Batch 3:" in turn
    assert content[-1]["cache_control"] == {"type": "ephemeral"}

    assert rolled_events(session) == [
        {
            "reason": "threshold",
            "before_tokens": 1600,
            "after_tokens": 800,
            "threshold_tokens": 1500,
            "dropped_turns": 4,
            "tail_segments": 2,
        }
    ]

    # The new conversation grows append-only again.
    await batch(loop, bus, session, 7)
    fourth = fake.requests[3]
    assert fourth.messages[0]["content"][:1] == [{"type": "text", "text": state}]
    assert len(fourth.messages) == 3 and fourth.messages[2]["content"][0]["type"] == "tool_result"

    # The dropped turns stay in the conversation file as history; a context record marks the drop.
    records = read_conversation(
        tmp_vault, session.subject_slug, session.topic_slug, f"observer-{session.id}"
    )
    kinds = [(record.kind, (record.detail or {}).get("reason")) for record in records]
    assert kinds == [
        ("context", "start"),
        ("user", None),
        ("assistant", None),
        ("user", None),
        ("assistant", None),
        ("context", "rollover"),
        ("user", None),
        ("assistant", None),
        ("user", None),
        ("assistant", None),
    ]
    assert "Frase número 1." in json.dumps(records[1].message, ensure_ascii=False)
    assert records[5].detail is not None and records[5].detail["before_tokens"] == 1600


async def test_a_rollover_keeps_the_catch_up_and_the_state_whole(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude, tmp_vault: Vault
) -> None:
    reply(
        fake,
        {"op": "add_section", "section_id": "sec-1", "title": "Membrana"},
        usage=Usage(input_tokens=2000, output_tokens=100),
    )
    reply(
        fake,
        {"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-1", "seg-3"]},
        usage=Usage(input_tokens=400),
    )
    await batch(loop, bus, session, 1)
    await batch(loop, bus, session, 3)
    assert len(fake.requests[1].messages) == 1  # rolled over after the first batch

    events = list(read_topic_events(tmp_vault, session.subject_slug, session.topic_slug))
    # Every batch was acknowledged: another session of the topic owes nothing.
    acks = [event.payload["through"] for _, event in events if event.kind == ACK_EVENT_KIND]
    assert len([through for through in acks if through is not None]) == 2
    owed = unanswered(events, session_id="20990101-000000", before=None)
    assert owed.acknowledged and owed.events == []
    # The ops of both conversations are folded into one state.
    state = load_observer_snapshot(tmp_vault, session.subject_slug, session.topic_slug).state
    assert state.segments_of("sec-1") == ["seg-1", "seg-3"]


async def test_session_end_always_drops_the_conversation(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude, tmp_vault: Vault
) -> None:
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert rolled_events(session) == []  # nothing was ever sent: nothing to drop

    reply(fake, usage=Usage(input_tokens=600, cache_read_input_tokens=200, output_tokens=40))
    await segment(bus, session, 1, "Solo una frase.")
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert len(fake.requests) == 1
    assert rolled_events(session) == [
        {
            "reason": "session_end",
            "before_tokens": 840,
            "after_tokens": 0,
            "threshold_tokens": 1500,
            "dropped_turns": 2,
            "tail_segments": 0,
        }
    ]
    records = read_conversation(
        tmp_vault, session.subject_slug, session.topic_slug, f"observer-{session.id}"
    )
    assert records[-1].kind == "context"
    assert records[-1].detail == {"reason": "session_end", "before_tokens": 840}

    # A second flush has nothing left to drop.
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert len(rolled_events(session)) == 1
