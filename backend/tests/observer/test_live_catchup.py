"""No batch is lost (#176): a batch the observer never got to answer is caught up later.

A slow Claude outlives the session end hook's timeout: the last batch's ops cannot land in the
ended session, so the next session of the topic sends it first. A stop in the middle of a session
(a crash, for the observer) leaves waiting items that the resume sends first. Every wait is
bounded, so a loop that never settles fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from studentassistant.config import LlmSettings, ObserverSettings, Settings
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.observer import load_observer_snapshot
from studentassistant.observer.catchup import ACK_EVENT_KIND
from studentassistant.observer.live import TOOL_NAME, ObserverLoop, default_client_factory
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.server.sessions import SessionService
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    read_topic_events,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        llm=LlmSettings(),
        observer=ObserverSettings(batch_segments=2, batch_speech_seconds=1000),
    )


class SlowTransport:
    """A `FakeClaude` whose answers wait for the test's gate (a Sonnet call slower than a hook)."""

    def __init__(self, fake: FakeClaude) -> None:
        self.fake = fake
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.answered = asyncio.Event()

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.entered.set()
        await self.gate.wait()
        try:
            return await self.fake.send(request)
        finally:
            self.answered.set()


async def _segment(bus: SessionBus, session_id: str, n: int, text: str) -> None:
    start = n * 10_000
    await bus.publish(
        session_id,
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


def _text(request: LLMRequest) -> str:
    blocks = request.messages[-1]["content"]
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


def _kinds(session: Session, kind: str, origin: str = "observer") -> list[dict[str, Any]]:
    return [dict(e.payload) for e in session.read_events() if e.kind == kind and e.origin == origin]


async def _until(condition: Any, what: str) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(poll(), WAIT)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


async def test_the_last_batch_of_a_call_outliving_the_end_hook_reaches_the_state(
    tmp_vault: Vault, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeClaude()
    slow = SlowTransport(fake)
    bus = SessionBus()
    service = SessionService(bus, vault=tmp_vault, host="pc-test", end_hook_timeout=0.05)
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, slow),
    )
    service.add_before_ended(loop.flush)
    loop.start()
    try:
        subject = await service.create_subject("Biología")
        topic = await service.create_topic(subject.subject_id, "La célula")
        first = await service.start(subject.subject_id, topic.topic_id, client_time_ms=1)
        # One final segment, below the trigger: only the end's flush sends it.
        await _segment(bus, first.session_id, 1, "Las mitocondrias producen energía.")
        await asyncio.wait_for(loop.drain(), WAIT)
        fake.reply_tool(
            TOOL_NAME,
            {"ops": [{"op": "add_section", "section_id": "sec-late", "title": "Tarde"}]},
        )
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(
                service.end(first.session_id, client_time_ms=2, reason="button"), WAIT
            )
            assert slow.entered.is_set() and not slow.answered.is_set()
            slow.gate.set()  # the call ends after the session did: its ops cannot land
            await asyncio.wait_for(slow.answered.wait(), WAIT)
            await _until(
                lambda: "batch not acknowledged" in caplog.text, "the refused acknowledgement"
            )
        assert [
            event.kind
            for session_id, event in read_topic_events(
                tmp_vault, subject.subject_id, topic.topic_id
            )
            if session_id == first.session_id and event.origin == "observer"
        ] == [ACK_EVENT_KIND]  # the baseline only: no op, no acknowledgement of the batch
        state = load_observer_snapshot(
            tmp_vault, subject.subject_id, topic.topic_id, write_back=False
        ).state
        assert "sec-late" not in state.sections

        # The next session of the topic catches the batch up before anything else.
        fake.reply_tool(
            TOOL_NAME,
            {
                "ops": [
                    {"op": "add_section", "section_id": "sec-mito", "title": "Mitocondrias"},
                    {"op": "assign_segments", "section_id": "sec-mito", "segment_ids": ["seg-1"]},
                ]
            },
        )
        second = await service.start(subject.subject_id, topic.topic_id, client_time_ms=3)
        await asyncio.wait_for(loop.wait_idle(second.session_id), WAIT)
        assert len(fake.requests) == 2
        text = _text(fake.requests[1])
        assert "catch-up: 1 events of session " + first.session_id in text
        assert "segment seg-1" in text and "Las mitocondrias producen energía." in text

        state = load_observer_snapshot(
            tmp_vault, subject.subject_id, topic.topic_id, write_back=False
        ).state
        assert state.segments_of("sec-mito") == ["seg-1"]
        live = await service.require_active(second.session_id)
        acks = _kinds(live, ACK_EVENT_KIND)
        seg_1 = next(
            event.seq
            for session_id, event in read_topic_events(
                tmp_vault, subject.subject_id, topic.topic_id
            )
            if session_id == first.session_id and event.kind == "transcript.final"
        )
        assert acks[-1] == {"through": {"session_id": first.session_id, "seq": seg_1}}

        # Answered: a later session of the topic owes nothing.
        await service.end(second.session_id, client_time_ms=4, reason="button")
        third = await service.start(subject.subject_id, topic.topic_id, client_time_ms=5)
        await asyncio.wait_for(loop.wait_idle(third.session_id), WAIT)
        assert len(fake.requests) == 2
    finally:
        slow.gate.set()
        await asyncio.wait_for(loop.stop(), WAIT)


async def test_a_resume_after_a_stop_sends_the_waiting_items_first(
    tmp_vault: Vault, settings: Settings
) -> None:
    subject = create_subject(tmp_vault, "Historia")
    topic = create_topic(tmp_vault, subject.slug, "Roma")
    session = start_session(tmp_vault, subject.slug, topic.slug, "pc", PROTOCOL_VERSION)
    fake = FakeClaude()

    def observer(bus: SessionBus) -> ObserverLoop:
        return ObserverLoop(
            bus,
            bus.attached,
            settings=settings.observer,
            client_factory=default_client_factory(settings, fake),
        )

    bus = SessionBus()
    bus.attach(session)
    first = observer(bus)
    first.start()
    try:
        await bus.publish(session.id, "session.started", "user", {})
        fake.reply_tool(TOOL_NAME, {"ops": []})
        await _segment(bus, session.id, 1, "La república.")
        await _segment(bus, session.id, 2, "El senado.")
        await asyncio.wait_for(first.wait_idle(session.id), WAIT)
        await _segment(bus, session.id, 3, "Julio César cruza el Rubicón.")
        await asyncio.wait_for(first.drain(), WAIT)
    finally:
        await asyncio.wait_for(first.stop(), WAIT)  # the backend dies: seg-3 was never sent
    assert len(fake.requests) == 1
    bus.detach(session.id)

    bus = SessionBus()
    bus.attach(session)
    second = observer(bus)
    second.start()
    try:
        fake.reply_tool(
            TOOL_NAME, {"ops": [{"op": "note", "text": "El Rubicón.", "segment_ids": ["seg-3"]}]}
        )
        await bus.publish(session.id, "session.resumed", "user", {})
        await asyncio.wait_for(second.wait_idle(session.id), WAIT)
    finally:
        await asyncio.wait_for(second.stop(), WAIT)

    assert len(fake.requests) == 2
    text = _text(fake.requests[1])
    assert "catch-up: 1 events" in text and "segment seg-3" in text
    assert "segment seg-1" not in text and "segment seg-2" not in text  # answered before
    state = load_observer_snapshot(tmp_vault, subject.slug, topic.slug, write_back=False).state
    assert [note.text for note in state.notes] == ["El Rubicón."]
    # The baseline, the first batch (through seg-2), the caught-up one (through seg-3).
    seqs = {
        e.payload["segment_id"]: e.seq
        for e in session.read_events()
        if e.kind == "transcript.final"
    }
    assert [ack["through"] for ack in _kinds(session, ACK_EVENT_KIND)] == [
        None,
        {"session_id": session.id, "seq": seqs["seg-2"]},
        {"session_id": session.id, "seq": seqs["seg-3"]},
    ]
