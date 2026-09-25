"""`ObserverLoop` on a real `SessionBus` and `tmp_vault`, with `FakeClaude` as the observer role.

Every wait is bounded (`asyncio.wait_for`), so a loop that never settles fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
import yaml

from studentassistant.config import LlmSettings, ObserverSettings, Settings
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse, Usage
from studentassistant.observer import STATE_OP_EVENT_KIND, load_observer_snapshot
from studentassistant.observer.live import (
    STATUS_EVENT_KIND,
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
    end_session,
    pending_review_path,
    read_conversation,
    read_ledger,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología")
    topic = create_topic(tmp_vault, subject.slug, "La célula")
    return subject.slug, topic.slug


@pytest.fixture
def session(tmp_vault: Vault, topic: tuple[str, str]) -> Session:
    return start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)


@pytest.fixture
def bus(session: Session) -> SessionBus:
    bus = SessionBus()
    bus.attach(session)
    return bus


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        llm=LlmSettings(),
        observer=ObserverSettings(batch_segments=2, batch_speech_seconds=1000),
    )


@pytest.fixture
async def loop(
    bus: SessionBus, fake: FakeClaude, settings: Settings
) -> AsyncIterator[ObserverLoop]:
    observer = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
    )
    observer.start()
    yield observer
    await asyncio.wait_for(observer.stop(), WAIT)


async def idle(loop: ObserverLoop, session_id: str) -> None:
    await asyncio.wait_for(loop.wait_idle(session_id), WAIT)


async def segment(
    bus: SessionBus, session: Session, n: int, text: str, *, seconds: float = 2.0
) -> None:
    start = n * 10_000
    await bus.publish(
        session.id,
        "transcript.final",
        "stt",
        {
            "segment_id": f"seg-{n}",
            "session_start_ms": start,
            "session_end_ms": start + int(seconds * 1000),
            "text": text,
            "provider": "fake",
        },
        t=start,
    )


def ops_reply(*ops: dict[str, Any]) -> dict[str, Any]:
    return {"ops": list(ops)}


def observer_ops(session: Session) -> list[dict[str, Any]]:
    return [
        dict(event.payload)
        for event in session.read_events()
        if event.kind == STATE_OP_EVENT_KIND and event.origin == "observer"
    ]


def user_text(request: LLMRequest) -> str:
    """Every text block of the newest user turn, joined."""
    blocks = request.messages[-1]["content"]
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


def all_text(request: LLMRequest) -> str:
    return json.dumps({"system": request.system, "messages": request.messages}, ensure_ascii=False)


async def test_two_segments_send_one_batch_whose_ops_are_published(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude, tmp_vault: Vault
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        ops_reply(
            {"op": "add_section", "section_id": "sec-1", "title": "Partes de la célula"},
            {"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-1", "seg-2"]},
        ),
        usage=Usage(input_tokens=1000, output_tokens=100),
    )
    await segment(bus, session, 1, "La célula es la unidad básica.")
    await asyncio.wait_for(loop.drain(), WAIT)
    assert fake.requests == []  # one segment is below the trigger
    await segment(bus, session, 2, "Tiene membrana, citoplasma y núcleo.")
    await idle(loop, session.id)

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.role == "observer"
    assert request.model == "claude-sonnet-5"
    assert request.tool_choice == {"type": "auto"}
    assert [tool["name"] for tool in request.tools] == [TOOL_NAME]
    assert request.tools[0]["strict"] is True
    # Stable prefix: the prompt, then the topic block carrying the cache breakpoint.
    assert "observer of a study session" in request.system[0]["text"]
    assert "Topic: La célula" in request.system[-1]["text"]
    assert request.system[-1]["cache_control"] == {"type": "ephemeral"}
    # The conversation opens with the state and the batch; its newest block is a breakpoint too.
    text = user_text(request)
    assert "record of this topic so far" in text
    assert "segment seg-1" in text and "segment seg-2" in text
    assert request.messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request.prompt_hash is not None and request.prompt_hash.startswith("sha256:")

    assert [op["op"] for op in observer_ops(session)] == ["add_section", "assign_segments"]
    assert loop.status(session.id) == "running"
    ledger = read_ledger(tmp_vault, session.subject_slug, session.topic_slug)
    assert [(entry.role, entry.session) for entry in ledger] == [("observer", session.id)]
    records = read_conversation(
        tmp_vault, session.subject_slug, session.topic_slug, f"observer-{session.id}"
    )
    assert [record.kind for record in records] == ["context", "user", "assistant"]
    assert records[2].usage is not None and records[2].usage["input_tokens"] == 1000
    assert records[2].prompt_hash == request.prompt_hash
    # The persisted turns carry no cache marker: it is added per request.
    assert "cache_control" not in json.dumps(records[1].message)


async def test_speech_seconds_trigger_a_batch(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    settings = Settings(observer=ObserverSettings(batch_segments=50, batch_speech_seconds=5))
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
    )
    loop.start()
    try:
        fake.reply_tool(TOOL_NAME, ops_reply())
        await segment(bus, session, 1, "uno", seconds=3)
        await asyncio.wait_for(loop.drain(), WAIT)
        assert fake.requests == []
        await segment(bus, session, 2, "dos", seconds=3)
        await idle(loop, session.id)
        assert len(fake.requests) == 1
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)


async def test_a_capture_and_a_source_switch_send_at_once(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        ops_reply({"op": "set_source_context", "kind": "book", "reference": "libro de texto"}),
    )
    fake.reply_tool(
        TOOL_NAME,
        ops_reply({"op": "link_capture", "capture_id": "cap-1", "segment_ids": ["seg-1"]}),
    )
    await segment(bus, session, 1, "En el libro pone que el núcleo guarda el ADN.")
    await bus.publish(session.id, "button", "phone", {"button": "switch_source", "source": "book"})
    await idle(loop, session.id)
    assert len(fake.requests) == 1
    assert "button switch_source" in user_text(fake.requests[0])

    await bus.publish(
        session.id,
        "capture.stored",
        "phone",
        {"capture_id": "cap-1", "trigger": "button", "source_context": "book"},
    )
    await idle(loop, session.id)
    assert len(fake.requests) == 2
    second = fake.requests[1]
    assert "capture cap-1" in user_text(second) and "source=book" in user_text(second)
    # Append-only: the first turn and its answer are kept, then the owed tool result comes first.
    assert second.messages[:2] == [
        {**fake.requests[0].messages[0], "content": second.messages[0]["content"]},
        second.messages[1],
    ]
    assert second.messages[1]["role"] == "assistant"
    assert second.messages[2]["content"][0]["type"] == "tool_result"
    assert second.messages[2]["content"][0]["tool_use_id"] == "toolu_fake_1"
    assert [op["op"] for op in observer_ops(session)] == ["set_source_context", "link_capture"]


class GatedTransport:
    """A `FakeClaude` whose first answer waits until the test opens the gate."""

    def __init__(self, fake: FakeClaude) -> None:
        self.fake = fake
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.entered.set()
        await self.gate.wait()
        return await self.fake.send(request)


async def test_items_arriving_during_a_call_coalesce_into_the_next_batch(
    bus: SessionBus, session: Session, fake: FakeClaude, settings: Settings
) -> None:
    gated = GatedTransport(fake)
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, gated),
    )
    loop.start()
    try:
        fake.reply_tool(TOOL_NAME, ops_reply())
        fake.reply_tool(TOOL_NAME, ops_reply())
        await segment(bus, session, 1, "uno")
        await segment(bus, session, 2, "dos")
        await asyncio.wait_for(gated.entered.wait(), WAIT)
        for n in (3, 4, 5, 6):  # two batches' worth, while the first call is in flight
            await segment(bus, session, n, f"número {n}")
        await asyncio.wait_for(loop.drain(), WAIT)
        gated.gate.set()
        await idle(loop, session.id)
        assert len(fake.requests) == 2
        text = user_text(fake.requests[1])
        assert all(f"segment seg-{n}" in text for n in (3, 4, 5, 6))
        assert "Batch 2:" in text
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)


async def test_invalid_ops_are_reasked_once_and_the_fix_published(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        ops_reply(
            {"op": "add_section", "section_id": "sec-1", "title": "Membrana"},
            {"op": "assign_segments", "section_id": "sec-9", "segment_ids": ["seg-1"]},
            {"op": "add_concept", "concept_id": "c-1"},
        ),
    )
    fake.reply_tool(
        TOOL_NAME,
        ops_reply({"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-1"]}),
    )
    await segment(bus, session, 1, "La membrana rodea la célula.")
    await segment(bus, session, 2, "Es semipermeable.")
    await idle(loop, session.id)

    assert len(fake.requests) == 2
    reask = fake.requests[1].messages[-1]["content"]
    assert reask[0]["type"] == "tool_result" and reask[0]["is_error"] is True
    assert "sec-9" in reask[0]["content"] and "op 2 is malformed" in reask[0]["content"]
    assert "Call `apply_state_ops` again" in reask[-1]["text"]
    assert observer_ops(session) == [
        {"op": "add_section", "section_id": "sec-1", "title": "Membrana", "parent_id": None},
        {"op": "assign_segments", "section_id": "sec-1", "segment_ids": ["seg-1"]},
    ]


async def test_ops_still_invalid_after_the_reask_are_dropped_and_logged(
    loop: ObserverLoop,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = {"op": "link_capture", "capture_id": "nope", "segment_ids": ["seg-1"]}
    fake.reply_tool(TOOL_NAME, ops_reply(bad))
    fake.reply_tool(TOOL_NAME, ops_reply(bad))
    fake.reply_tool(TOOL_NAME, ops_reply({"op": "note", "text": "sigue", "segment_ids": []}))
    with caplog.at_level(logging.WARNING, logger="studentassistant.observer.live"):
        await segment(bus, session, 1, "uno")
        await segment(bus, session, 2, "dos")
        await idle(loop, session.id)
    assert len(fake.requests) == 2
    assert observer_ops(session) == []
    assert any("dropped after one re-ask" in record.message for record in caplog.records)

    # The next batch answers the re-ask's tool call before carrying its own lines.
    await segment(bus, session, 3, "tres")
    await segment(bus, session, 4, "cuatro")
    await idle(loop, session.id)
    third = fake.requests[2].messages[-1]["content"]
    assert third[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_fake_2",
        "content": "Applied 0 ops.",
    }
    assert [op["op"] for op in observer_ops(session)] == ["note"]


async def test_an_answer_without_the_tool_is_reasked_then_dropped(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("Entendido.")
    fake.reply_text("Vale.")
    await segment(bus, session, 1, "uno")
    await segment(bus, session, 2, "dos")
    await idle(loop, session.id)
    assert len(fake.requests) == 2
    assert "did not call the `apply_state_ops` tool" in user_text(fake.requests[1])
    assert observer_ops(session) == []


async def test_a_cost_cap_pauses_the_observer_and_keeps_the_batch(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    settings = Settings(
        llm=LlmSettings(max_usd_per_session=0.0),
        observer=ObserverSettings(batch_segments=1, batch_speech_seconds=1000),
    )
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
    )
    loop.start()
    try:
        await segment(bus, session, 1, "uno")
        await idle(loop, session.id)
        assert fake.requests == []  # nothing is sent over a cap
        assert loop.status(session.id) == "paused"
        statuses = [e.payload for e in session.read_events() if e.kind == STATUS_EVENT_KIND]
        assert len(statuses) == 1
        assert statuses[0]["status"] == "paused" and statuses[0]["cap"] == "session"

        await segment(bus, session, 2, "dos")  # still paused: no second status event
        await idle(loop, session.id)
        assert len([e for e in session.read_events() if e.kind == STATUS_EVENT_KIND]) == 1

        # The cap is lifted: the next trigger sends everything kept, and the observer runs again.
        loop._observed[session.id].client.llm_settings = LlmSettings()
        fake.reply_tool(TOOL_NAME, ops_reply())
        await segment(bus, session, 3, "tres")
        await idle(loop, session.id)
        assert len(fake.requests) == 1
        text = user_text(fake.requests[0])
        assert all(f"segment seg-{n}" in text for n in (1, 2, 3))
        assert loop.status(session.id) == "running"
        statuses = [e.payload for e in session.read_events() if e.kind == STATUS_EVENT_KIND]
        assert [s["status"] for s in statuses] == ["paused", "running"]
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)


async def test_flush_sends_what_is_below_the_trigger(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_tool(TOOL_NAME, ops_reply({"op": "note", "text": "Fin del repaso."}))
    await segment(bus, session, 1, "Y con esto termino.")
    await asyncio.wait_for(loop.drain(), WAIT)
    assert fake.requests == []
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert len(fake.requests) == 1
    assert [op["op"] for op in observer_ops(session)] == ["note"]
    # Nothing waiting: a second flush makes no call.
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert len(fake.requests) == 1


async def test_a_new_session_rebuilds_its_context_from_the_snapshot_of_its_topic_only(
    tmp_vault: Vault, topic: tuple[str, str], fake: FakeClaude, settings: Settings
) -> None:
    # An earlier session of the topic, and a session of another topic, with their own ops.
    other_subject = create_subject(tmp_vault, "Historia")
    other_topic = create_topic(tmp_vault, other_subject.slug, "Roma")
    for slugs, title, words in (
        (topic, "Orgánulos", "el retículo endoplasmático"),
        ((other_subject.slug, other_topic.slug), "Imperio romano", "Julio César"),
    ):
        earlier = start_session(tmp_vault, *slugs, "pc", PROTOCOL_VERSION)
        earlier.append_event("transcript.final", "stt", {"segment_id": "old-1", "text": words})
        earlier.append_event(
            STATE_OP_EVENT_KIND,
            "observer",
            {"op": "add_section", "section_id": "sec-old", "title": title},
        )
        end_session(earlier)

    session = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    bus = SessionBus()
    bus.attach(session)
    loop = ObserverLoop(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
    )
    loop.start()
    try:
        await bus.publish(session.id, "session.resumed", "user", {})
        fake.reply_tool(
            TOOL_NAME,
            ops_reply({"op": "assign_segments", "section_id": "sec-old", "segment_ids": ["seg-1"]}),
        )
        await segment(bus, session, 1, "Seguimos con los orgánulos.")
        await segment(bus, session, 2, "Las mitocondrias.")
        await idle(loop, session.id)
    finally:
        await asyncio.wait_for(loop.stop(), WAIT)

    request = fake.requests[0]
    text = all_text(request)
    assert "resumes now" in text
    assert "sec-old: Orgánulos" in text  # from the snapshot of this topic
    assert "retículo" not in text  # the earlier session's words are not replayed
    assert "Imperio romano" not in text and "Julio César" not in text and "Roma" not in text
    assert len(request.messages) == 1
    # The op referencing the snapshot's section was accepted.
    assert [op["op"] for op in observer_ops(session)] == ["assign_segments"]
    state = load_observer_snapshot(tmp_vault, *topic, write_back=False).state
    assert state.segments_of("sec-old") == ["seg-1"]


async def test_session_end_forgets_the_session(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    await segment(bus, session, 1, "uno")
    await asyncio.wait_for(loop.drain(), WAIT)
    assert loop.status(session.id) == "running"
    await bus.publish(session.id, "session.ended", "user", {})
    await asyncio.wait_for(loop.drain(), WAIT)
    assert loop.status(session.id) is None
    await asyncio.wait_for(loop.flush(session.id), WAIT)
    assert fake.requests == []


async def test_pending_changes_publish_the_open_count_and_regenerate_the_review(
    loop: ObserverLoop, bus: SessionBus, session: Session, fake: FakeClaude, tmp_vault: Vault
) -> None:
    subscription = bus.subscribe(name="phone", session_id=session.id, kinds={"notice"})
    counts: list[int] = []

    async def collect() -> None:
        async for event in subscription:
            counts.append(event.payload["pending_count"])

    collector = asyncio.create_task(collect())

    async def seen(n: int) -> list[int]:
        async def until() -> None:
            while len(counts) < n:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(until(), WAIT)
        await asyncio.sleep(0.05)  # nothing more arrives
        return counts

    review_path = pending_review_path(tmp_vault, session.subject_slug, session.topic_slug)
    fake.reply_tool(
        TOOL_NAME,
        ops_reply(
            {
                "op": "add_pending",
                "pending_id": "p-1",
                "kind": "illegible",
                "text": "No se lee la palabra junto a la fórmula",
                "segment_ids": ["seg-1"],
                "capture_ids": [],
                "source_refs": [],
            },
            {"op": "add_section", "section_id": "sec-1", "title": "La membrana"},
        ),
    )
    fake.reply_tool(
        TOOL_NAME,
        ops_reply(
            {
                "op": "add_pending",
                "pending_id": "p-2",
                "kind": "illegible",
                "text": "No se lee la palabra junto a la fórmula",
                "segment_ids": ["seg-3"],
                "capture_ids": [],
                "source_refs": [],
            }
        ),
    )
    try:
        await segment(bus, session, 1, "Aquí pone algo que no entiendo.")
        await segment(bus, session, 2, "La membrana rodea la célula.")
        await idle(loop, session.id)
        # The count at the start, then after the new item; the section changes nothing pending.
        assert await seen(2) == [0, 1]
        review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
        assert review["open_count"] == 1
        assert [item["id"] for item in review["items"]] == ["p-1"]

        await segment(bus, session, 3, "Sigo sin leer esa palabra.")
        await segment(bus, session, 4, "Pasamos al núcleo.")
        await idle(loop, session.id)
        # Merged into p-1: the queue changed (its refs) but still holds one open doubt.
        assert await seen(3) == [0, 1, 1]
        review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
        assert review["items"][0]["merged_ids"] == ["p-2"]
        assert review["items"][0]["refs"]["segments"] == ["seg-1", "seg-3"]

        # The student settles it (any origin): the count drops to zero.
        await bus.publish(
            session.id,
            STATE_OP_EVENT_KIND,
            "user",
            {"op": "resolve_pending", "pending_id": "p-2", "resolution": "Dice «fosfolípidos»"},
        )
        await asyncio.wait_for(loop.drain(), WAIT)
        assert await seen(4) == [0, 1, 1, 0]
        review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
        assert review["open_count"] == 0
        assert review["items"][0]["status"] == "resolved"
        # A notice is transient: never in the event log.
        assert all(event.kind != "notice" for event in session.read_events())
    finally:
        subscription.close()
        await asyncio.wait_for(collector, WAIT)
