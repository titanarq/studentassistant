"""`RequestDetector` on a real `SessionBus` and `tmp_vault`, `FakeClaude` as the observer role and a
fake clock driving the debounce and max-wait triggers.

Every wait is bounded (`asyncio.wait_for`), so a detector that never settles fails instead of
hanging.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from studentassistant.config import LlmSettings, ObserverSettings, Settings
from studentassistant.llm import FakeClaude, LLMAPIError, LLMRequest, load_prompt
from studentassistant.observer import (
    ASSISTANT_REQUEST_KIND,
    REQUEST_KINDS,
    AskedDoubtRef,
    AssistantRequest,
    RequestContext,
    RequestSource,
    fold,
)
from studentassistant.observer.live import STATUS_EVENT_KIND, default_client_factory
from studentassistant.observer.requests import (
    MESSAGES_CONVERSATION,
    TOOL_NAME,
    ClassificationError,
    MessageClassifier,
    RequestDetector,
)
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.stt import CommandDetector, load_grammar
from studentassistant.vault import (
    ConversationRecord,
    Session,
    Vault,
    append_conversation_record,
    create_subject,
    create_topic,
    read_conversation,
    read_ledger,
    read_topic_events,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 5.0


class FakeClock:
    """Monotonic time that only moves with `advance`; `sleep` waits for it."""

    def __init__(self) -> None:
        self.time = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return datetime(2026, 9, 26, 10, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.time + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self.time += seconds
        waiting = []
        for deadline, future in self._sleepers:
            if future.done():
                continue
            if deadline <= self.time:
                future.set_result(None)
            else:
                waiting.append((deadline, future))
        self._sleepers = waiting


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
def clock() -> FakeClock:
    return FakeClock()


def make_detector(
    bus: SessionBus, fake: FakeClaude, clock: FakeClock, settings: Settings | None = None
) -> RequestDetector:
    settings = settings or Settings(llm=LlmSettings(), observer=ObserverSettings())
    return RequestDetector(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
        clock=clock,
    )


@pytest.fixture
async def detector(
    bus: SessionBus, fake: FakeClaude, clock: FakeClock
) -> AsyncIterator[RequestDetector]:
    detector = make_detector(bus, fake, clock)
    detector.start()
    yield detector
    await asyncio.wait_for(detector.stop(), WAIT)


async def segment(bus: SessionBus, session: Session, n: int, text: str) -> None:
    start = n * 3_000
    await bus.publish(
        session.id,
        "transcript.final",
        "stt",
        {
            "segment_id": f"seg-{n}",
            "session_start_ms": start,
            "session_end_ms": start + 2_500,
            "text": text,
            "provider": "fake",
        },
        t=start,
    )


async def settle(detector: RequestDetector, session: Session) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.wait_for(detector.wait_idle(session.id), WAIT)


async def advance(
    detector: RequestDetector, clock: FakeClock, session: Session, seconds: float
) -> None:
    await asyncio.wait_for(detector.drain(), WAIT)
    clock.advance(seconds)
    await settle(detector, session)


def report(*requests: dict[str, Any]) -> dict[str, Any]:
    return {"requests": list(requests)}


def requests_of(session: Session) -> list[dict[str, Any]]:
    return [
        {"origin": event.origin, **event.payload}
        for event in session.read_events()
        if event.kind == ASSISTANT_REQUEST_KIND
    ]


def user_text(request: LLMRequest) -> str:
    blocks = request.messages[-1]["content"]
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


def window_text(request: LLMRequest) -> str:
    blocks = request.messages[0]["content"]
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


DICTATION = [
    "La célula es la unidad básica de la vida.",
    "Tiene membrana, citoplasma y núcleo.",
]
REQUEST = "Oye, pon esto último como una definición."


async def test_a_request_after_dictation_is_detected_once_with_its_span(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
    tmp_vault: Vault,
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        report(
            {"kind": "edit", "summary": "Poner lo último como definición", "segment_ids": ["seg-3"]}
        ),
    )
    for n, text in enumerate([*DICTATION, REQUEST], start=1):
        await segment(bus, session, n, text)
    await advance(detector, clock, session, 1.0)
    assert fake.requests == []  # within the debounce
    await advance(detector, clock, session, 0.6)

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.role == "observer"
    assert [tool["name"] for tool in request.tools] == [TOOL_NAME]
    assert request.tools[0]["strict"] is True
    assert request.tool_choice == {"type": "auto"}
    assert "request to the assistant" in request.system[0]["text"]
    assert "Topic: La célula" in request.system[-1]["text"]
    text = user_text(request)
    assert "seg-1 [3.0s-5.5s] new La célula" in text
    assert "seg-3" in text and REQUEST in text

    events = requests_of(session)
    assert events == [
        {
            "origin": "observer",
            "request_id": "req-1",
            "kind": "edit",
            "summary": "Poner lo último como definición",
            "text": REQUEST,
            "segment_ids": ["seg-3"],
            "t_start_ms": 9_000,
            "t_end_ms": 11_500,
            "detector": "observer",
        }
    ]
    AssistantRequest.model_validate({k: v for k, v in events[0].items() if k != "origin"})

    # No new final: nothing is examined again.
    await advance(detector, clock, session, 20)
    assert len(fake.requests) == 1

    ledger = read_ledger(tmp_vault, session.subject_slug, session.topic_slug)
    assert [(entry.role, entry.session) for entry in ledger] == [("observer", session.id)]
    records = read_conversation(
        tmp_vault, session.subject_slug, session.topic_slug, f"observer-requests-{session.id}"
    )
    assert [record.kind for record in records] == ["context", "user", "assistant"]
    assert records[0].detail is not None and records[0].detail["reason"] == "start"
    assert records[2].prompt_hash == request.prompt_hash


async def test_a_multi_segment_request_joins_the_raw_text(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        report(
            {
                "kind": "question",
                "summary": "Diferencia entre mitosis y meiosis",
                "segment_ids": ["seg-2", "seg-3"],
            }
        ),
    )
    await segment(bus, session, 1, DICTATION[0])
    await segment(bus, session, 2, "Oye, una pregunta:")
    await segment(bus, session, 3, "¿qué diferencia hay entre mitosis y meiosis?")
    await advance(detector, clock, session, 2)
    [event] = requests_of(session)
    assert event["text"] == "Oye, una pregunta: ¿qué diferencia hay entre mitosis y meiosis?"
    assert (event["t_start_ms"], event["t_end_ms"]) == (6_000, 11_500)


async def test_plain_dictation_yields_no_request(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
) -> None:
    fake.reply_tool(TOOL_NAME, report())
    for n, text in enumerate(DICTATION, start=1):
        await segment(bus, session, n, text)
    await advance(detector, clock, session, 2)
    assert len(fake.requests) == 1
    assert requests_of(session) == []


async def test_assigned_segments_are_marked_and_never_reported_twice(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    edit = {"kind": "edit", "summary": "Poner como definición", "segment_ids": ["seg-2"]}
    fake.reply_tool(TOOL_NAME, report(edit))
    await segment(bus, session, 1, DICTATION[0])
    await segment(bus, session, 2, REQUEST)
    await advance(detector, clock, session, 2)
    assert len(requests_of(session)) == 1

    # The next window shows seg-1 as seen and seg-2 as part of req-1; reporting seg-2 again is
    # refused, re-asked once, and then dropped.
    fake.reply_tool(TOOL_NAME, report(edit))
    fake.reply_tool(TOOL_NAME, report(edit))
    await segment(bus, session, 3, DICTATION[1])
    with caplog.at_level(logging.WARNING, logger="studentassistant.observer.requests"):
        await advance(detector, clock, session, 2)
    assert len(fake.requests) == 3
    window = window_text(fake.requests[1])
    assert "seg-1 [3.0s-5.5s] seen" in window
    assert "seg-2 [6.0s-8.5s] req-1" in window
    assert "seg-3 [9.0s-11.5s] new" in window
    assert "already part of a request: seg-2" in user_text(fake.requests[2])
    assert fake.requests[2].messages[1]["role"] == "assistant"
    assert len(requests_of(session)) == 1
    assert any("dropped after one re-ask" in r.message for r in caplog.records)


async def test_an_invalid_span_is_re_asked_and_the_correction_published(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        report(
            {"kind": "edit", "summary": "x" * 141, "segment_ids": ["seg-2"]},
            {"kind": "edit", "summary": "Hueco", "segment_ids": ["seg-1", "seg-3"]},
            {"kind": "edit", "summary": "Fuera", "segment_ids": ["seg-9"]},
        ),
    )
    fake.reply_tool(
        TOOL_NAME,
        report({"kind": "edit", "summary": "Poner como definición", "segment_ids": ["seg-2"]}),
    )
    for n, text in enumerate([DICTATION[0], REQUEST, DICTATION[1]], start=1):
        await segment(bus, session, n, text)
    await advance(detector, clock, session, 2)
    assert len(fake.requests) == 2
    retry = user_text(fake.requests[1])
    assert "at most 140" in retry
    assert "consecutive" in retry
    assert "not in the window: seg-9" in retry
    tool_result = fake.requests[1].messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["is_error"] is True
    assert [e["segment_ids"] for e in requests_of(session)] == [["seg-2"]]


async def test_max_wait_triggers_while_the_student_keeps_talking(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
) -> None:
    fake.reply_tool(TOOL_NAME, report())
    # A final every second: the 1.5 s debounce never elapses, the 8 s max wait does.
    for n in range(1, 8):
        await segment(bus, session, n, f"Frase {n}.")
        await advance(detector, clock, session, 1.0)
    assert fake.requests == []
    await segment(bus, session, 8, "Frase 8.")
    await advance(detector, clock, session, 1.0)
    assert len(fake.requests) == 1
    assert "8 new" in user_text(fake.requests[0])


async def test_finals_during_a_call_coalesce_into_the_next_one(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    detector = make_detector(bus, fake, clock)
    detector.start()
    gate = asyncio.Event()
    send = fake.send

    async def slow_send(request: LLMRequest, on_text: Any = None) -> Any:
        if len(fake.requests) == 0:
            await asyncio.wait_for(gate.wait(), WAIT)
        return await send(request, on_text)

    fake.send = slow_send  # type: ignore[method-assign]
    try:
        fake.reply_tool(TOOL_NAME, report()).reply_tool(TOOL_NAME, report())
        await segment(bus, session, 1, "uno")
        await asyncio.wait_for(detector.drain(), WAIT)
        clock.advance(2)
        for _ in range(5):
            await asyncio.sleep(0)
        await segment(bus, session, 2, "dos")
        await segment(bus, session, 3, "tres")
        await asyncio.wait_for(detector.drain(), WAIT)
        clock.advance(2)  # due, but a call is in flight
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await settle(detector, session)
        assert len(fake.requests) == 2
        assert "1 new" in user_text(fake.requests[0])
        second = user_text(fake.requests[1])
        assert "2 new" in second and "seg-1 [3.0s-5.5s] seen" in second
    finally:
        gate.set()
        await asyncio.wait_for(detector.stop(), WAIT)


async def test_off_makes_no_call_and_publishes_nothing(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    settings = Settings(observer=ObserverSettings(request_detection="off"))
    detector = make_detector(bus, fake, clock, settings)
    detector.start()
    assert not detector.running
    await segment(bus, session, 1, "anel, pon esto como definición")
    await wake_word_command(bus, session, 1, "pon esto como definición")
    clock.advance(20)
    await asyncio.sleep(0)
    await asyncio.wait_for(detector.flush(session.id), WAIT)
    await asyncio.wait_for(detector.stop(), WAIT)
    assert fake.requests == []
    assert requests_of(session) == []


# -- wake word (#318) ----------------------------------------------------------------------------


async def wake_word_command(
    bus: SessionBus, session: Session, n: int, query: str, command: str = "assistant_request"
) -> None:
    """The `voice.command` the stt detector publishes for "anel, <query>" in segment `n`."""
    await bus.publish(
        session.id,
        "voice.command",
        "stt",
        {"command": command, "segment_id": f"seg-{n}", "text": f"anel, {query}", "query": query},
        t=n * 3_000,
    )


def wake_word_detector(bus: SessionBus, fake: FakeClaude, clock: FakeClock) -> RequestDetector:
    settings = Settings(observer=ObserverSettings(request_detection="wake_word"))
    return make_detector(bus, fake, clock, settings)


async def test_wake_word_turns_the_command_into_a_request_without_claude(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    detector = wake_word_detector(bus, fake, clock)
    detector.start()
    try:
        assert detector.running
        await segment(bus, session, 1, DICTATION[0])
        await segment(bus, session, 2, "anel, haz una tabla con las causas")
        await wake_word_command(bus, session, 2, "haz una tabla con las causas")
        clock.advance(30)
        await asyncio.wait_for(detector.flush(session.id), WAIT)
        assert requests_of(session) == [
            {
                "origin": "stt",
                "request_id": "req-1",
                "kind": "edit",
                "summary": "haz una tabla con las causas",
                "text": "haz una tabla con las causas",
                "segment_ids": ["seg-2"],
                "t_start_ms": 6_000,
                "t_end_ms": 8_500,
                "detector": "wake_word",
            }
        ]
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    assert fake.requests == []


async def test_wake_word_prepare_notes_long_summary_and_other_commands(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    long_query = "explica " + " ".join(["la fase de la mitosis"] * 10)
    detector = wake_word_detector(bus, fake, clock)
    detector.start()
    try:
        await segment(bus, session, 1, "anel, prepárame el tema")
        await wake_word_command(bus, session, 1, "prepárame el tema, por favor")
        await segment(bus, session, 2, "busca en internet la meiosis")
        await wake_word_command(bus, session, 2, "la meiosis", command="web_search")
        await segment(bus, session, 3, f"anel, {long_query}")
        await wake_word_command(bus, session, 3, long_query)
        await wake_word_command(bus, session, 3, long_query)  # never twice for one segment
        await wake_word_command(bus, session, 4, "   ")  # an empty query fires nothing
        await asyncio.wait_for(detector.flush(session.id), WAIT)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    found = requests_of(session)
    assert [(e["request_id"], e["kind"]) for e in found] == [
        ("req-1", "prepare_notes"),
        ("req-2", "edit"),
    ]
    summary = found[1]["summary"]
    assert len(summary) <= 140
    assert long_query.startswith(summary)
    assert long_query[len(summary)] == " "  # cut at a word boundary
    assert found[1]["text"] == long_query
    assert fake.requests == []


async def test_wake_word_keeps_the_numbering_of_a_resumed_session(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    await bus.publish(
        session.id,
        ASSISTANT_REQUEST_KIND,
        "stt",
        {
            "request_id": "req-1",
            "kind": "edit",
            "summary": "Antes",
            "text": "Antes",
            "segment_ids": ["seg-1"],
            "t_start_ms": 3_000,
            "t_end_ms": 5_500,
            "detector": "wake_word",
        },
    )
    detector = wake_word_detector(bus, fake, clock)
    detector.start()
    try:
        await wake_word_command(bus, session, 1, "otra vez")  # already a request
        await segment(bus, session, 2, "anel pon un ejemplo")
        await wake_word_command(bus, session, 2, "pon un ejemplo")
        await asyncio.wait_for(detector.flush(session.id), WAIT)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    assert [e["request_id"] for e in requests_of(session)] == ["req-1", "req-2"]


async def test_observer_mode_ignores_the_wake_word_command(
    detector: RequestDetector, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    await wake_word_command(bus, session, 1, "pon un ejemplo")
    await asyncio.wait_for(detector.flush(session.id), WAIT)
    assert fake.requests == []
    assert requests_of(session) == []


async def test_wake_word_end_to_end_from_the_transcript(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    commands = CommandDetector(bus, load_grammar())
    detector = wake_word_detector(bus, fake, clock)
    commands.start()
    detector.start()
    try:
        await segment(bus, session, 1, "Daniel dijo que la mitosis tiene cuatro fases")
        await segment(bus, session, 2, "Anel, pon esto como definición.")
        await asyncio.wait_for(commands.drain(), WAIT)
        await asyncio.wait_for(detector.flush(session.id), WAIT)
    finally:
        await asyncio.wait_for(commands.stop(), WAIT)
        await asyncio.wait_for(detector.stop(), WAIT)
    found = requests_of(session)
    assert [(e["kind"], e["text"], e["segment_ids"]) for e in found] == [
        ("edit", "pon esto como definición.", ["seg-2"])
    ]
    assert fake.requests == []


async def test_a_cost_cap_pauses_detection_until_a_new_final(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    settings = Settings(llm=LlmSettings(max_usd_per_session=0.0))
    detector = make_detector(bus, fake, clock, settings)
    detector.start()
    try:
        await segment(bus, session, 1, REQUEST)
        await advance(detector, clock, session, 2)
        assert fake.requests == []
        assert detector.status(session.id) == "paused"
        statuses = [e.payload for e in session.read_events() if e.kind == STATUS_EVENT_KIND]
        assert len(statuses) == 1
        assert statuses[0]["status"] == "paused" and statuses[0]["cap"] == "session"
        assert statuses[0]["detector"] == "requests"
        # No new final: never retried in a loop.
        await advance(detector, clock, session, 20)
        assert fake.requests == []

        detector._detected[session.id].client.llm_settings = LlmSettings()
        fake.reply_tool(
            TOOL_NAME,
            report({"kind": "edit", "summary": "Definición", "segment_ids": ["seg-1"]}),
        )
        await segment(bus, session, 2, "vale")
        await advance(detector, clock, session, 2)
        assert len(fake.requests) == 1
        assert "2 new" in user_text(fake.requests[0])  # the kept final is examined too
        assert detector.status(session.id) == "running"
        statuses = [e.payload for e in session.read_events() if e.kind == STATUS_EVENT_KIND]
        assert [(s["status"], s["detector"]) for s in statuses] == [
            ("paused", "requests"),
            ("running", "requests"),
        ]
        assert len(requests_of(session)) == 1
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)


async def test_flush_examines_a_request_spoken_just_before_ending(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        report({"kind": "prepare_notes", "summary": "Preparar el tema", "segment_ids": ["seg-1"]}),
    )
    await segment(bus, session, 1, "Prepárame el tema.")
    await asyncio.wait_for(detector.drain(), WAIT)
    assert fake.requests == []  # the debounce has not elapsed
    await asyncio.wait_for(detector.flush(session.id), WAIT)
    assert len(fake.requests) == 1
    assert "The session is ending" in user_text(fake.requests[0])
    assert [e["kind"] for e in requests_of(session)] == ["prepare_notes"]


async def test_a_resumed_session_keeps_numbering_and_assignments(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    await bus.publish(
        session.id,
        ASSISTANT_REQUEST_KIND,
        "observer",
        {
            "request_id": "req-1",
            "kind": "edit",
            "summary": "Antes",
            "text": REQUEST,
            "segment_ids": ["seg-1"],
            "t_start_ms": 3_000,
            "t_end_ms": 5_500,
            "detector": "observer",
        },
    )
    detector = make_detector(bus, fake, clock)
    detector.start()
    try:
        fake.reply_tool(
            TOOL_NAME,
            report({"kind": "question", "summary": "Una duda", "segment_ids": ["seg-2"]}),
        )
        await segment(bus, session, 2, "¿Esto está bien?")
        await advance(detector, clock, session, 2)
        assert [e["request_id"] for e in requests_of(session)] == ["req-1", "req-2"]
        records = read_conversation(
            session.vault,
            session.subject_slug,
            session.topic_slug,
            f"observer-requests-{session.id}",
        )
        assert records[0].detail is not None and records[0].detail["requests"] == 1
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)


async def test_the_fold_ignores_assistant_requests(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
    tmp_vault: Vault,
) -> None:
    fake.reply_tool(
        TOOL_NAME, report({"kind": "edit", "summary": "Definición", "segment_ids": ["seg-1"]})
    )
    await segment(bus, session, 1, REQUEST)
    await advance(detector, clock, session, 2)
    events = list(read_topic_events(tmp_vault, session.subject_slug, session.topic_slug))
    assert any(event.kind == ASSISTANT_REQUEST_KIND for _, event in events)
    without = [(s, e) for s, e in events if e.kind != ASSISTANT_REQUEST_KIND]
    assert fold(events) == fold(without)


def test_assistant_request_model_validates_the_payload() -> None:
    good = {
        "request_id": "req-3",
        "kind": "edit",
        "summary": "Hacer una tabla",
        "text": "haz una tabla",
        "segment_ids": ["s-1"],
        "t_start_ms": 10,
        "t_end_ms": 20,
        "detector": "wake_word",
    }
    assert AssistantRequest.model_validate(good).kind == "edit"
    for change in (
        {"kind": "delete"},
        {"summary": "x" * 141},
        {"segment_ids": []},
        {"t_end_ms": 5},
        {"request_id": "r3"},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            AssistantRequest.model_validate(good | change)
    assert json.loads(AssistantRequest.model_validate(good).model_dump_json())["detector"] == (
        "wake_word"
    )


def test_request_detection_config_keys() -> None:
    settings = ObserverSettings()
    assert settings.request_detection == "observer"
    assert settings.request_debounce_seconds == 1.5
    assert settings.request_max_wait_seconds == 8
    assert settings.request_window_segments == 12
    with pytest.raises(ValidationError):
        ObserverSettings(request_detection="anel")  # type: ignore[arg-type]


def test_request_detection_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_OBSERVER__REQUEST_DETECTION", "wake_word")
    monkeypatch.setenv("SA_CONFIG", "/nonexistent/config.toml")
    assert Settings().observer.request_detection == "wake_word"


# -- chat-driven kinds (#327) ----------------------------------------------------------------------

PAGE_ID = "sources/notes/page-00{n}.jpg"


def page_source(n: int, state: str = "pendiente", reason: str | None = None) -> RequestSource:
    return RequestSource(
        source_id=PAGE_ID.format(n=n),
        kind="notes",
        number=n,
        label=f"la página {n}",
        state=state,  # type: ignore[arg-type]
        reason=reason,
    )


CONTEXT = RequestContext(
    sources=[
        page_source(1, "incorporada"),
        page_source(2, "apartada", "borrosa"),
        page_source(3),
        page_source(4),
        RequestSource(
            source_id="sources/pdf/tema.pdf",
            kind="pdf",
            label="el PDF «tema.pdf»",
            state="pendiente",
        ),
    ]
)
ASKED = AskedDoubtRef(pending_id="p-7", question="¿Qué pone?", suggestions=["escrita", "escrito"])


def context_detector(
    bus: SessionBus,
    fake: FakeClaude,
    clock: FakeClock,
    context: RequestContext,
    *,
    llm: LlmSettings | None = None,
) -> tuple[RequestDetector, list[tuple[str, str]]]:
    asked: list[tuple[str, str]] = []

    async def lookup(subject: str, topic: str) -> RequestContext:
        asked.append((subject, topic))
        return context

    settings = Settings(llm=llm or LlmSettings(), observer=ObserverSettings())
    detector = RequestDetector(
        bus,
        bus.attached,
        settings=settings.observer,
        client_factory=default_client_factory(settings, fake),
        clock=clock,
        sources_lookup=lookup,
    )
    return detector, asked


async def test_the_two_last_pages_are_resolved_from_the_sources_list(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    detector, asked = context_detector(bus, fake, clock, CONTEXT)
    detector.start()
    try:
        fake.reply_tool(
            TOOL_NAME,
            report(
                {
                    "kind": "incorporate",
                    "summary": "Incorporar las páginas 3 y 4",
                    "segment_ids": ["seg-1"],
                    "targets": [PAGE_ID.format(n=3), PAGE_ID.format(n=4), PAGE_ID.format(n=3)],
                    "answer": "ignored",
                }
            ),
        )
        await segment(bus, session, 1, "incorpora las dos últimas")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)

    assert asked == [(session.subject_slug, session.topic_slug)]
    text = user_text(fake.requests[0])
    assert "sources/notes/page-002.jpg: página 2 (apuntes) -- apartada: borrosa" in text
    assert "sources/notes/page-001.jpg: página 1 (apuntes) -- incorporada" in text
    assert "sources/pdf/tema.pdf: PDF «tema.pdf» (PDF) -- pendiente" in text
    assert "Doubt asked in the chat now: none." in text
    assert text.index("Sources of the topic") < text.index("seg-1")
    # The context is in the user turn, after the cached system prefix.
    assert "Sources of the topic" not in str(fake.requests[0].system)
    [event] = requests_of(session)
    assert event["kind"] == "incorporate"
    assert event["targets"] == [PAGE_ID.format(n=3), PAGE_ID.format(n=4)]
    assert "answer" not in event and "pending_id" not in event


async def test_an_unresolvable_reference_is_re_asked_and_becomes_a_question(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    detector, _ = context_detector(bus, fake, clock, CONTEXT)
    detector.start()
    try:
        fake.reply_tool(
            TOOL_NAME,
            report(
                {
                    "kind": "incorporate",
                    "summary": "Incorporar la página 9",
                    "segment_ids": ["seg-1"],
                    "targets": [PAGE_ID.format(n=9)],
                },
                {
                    "kind": "set_aside",
                    "summary": "Apartar el PDF",
                    "segment_ids": ["seg-2"],
                    "targets": ["sources/pdf/tema.pdf"],
                },
                {"kind": "restore", "summary": "Recuperar", "segment_ids": ["seg-3"]},
            ),
        )
        fake.reply_tool(
            TOOL_NAME,
            report(
                {
                    "kind": "question",
                    "summary": "No hay página 9: ¿cuál quieres incorporar?",
                    "segment_ids": ["seg-1"],
                }
            ),
        )
        await segment(bus, session, 1, "incorpora la nueve")
        await segment(bus, session, 2, "y aparta el pdf")
        await segment(bus, session, 3, "y recupera esa")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)

    retry = user_text(fake.requests[1])
    assert "targets not in the sources list: sources/notes/page-009.jpg" in retry
    assert "only captured pages" in retry
    assert "a restore request needs its targets" in retry
    assert [(e["kind"], e["request_id"]) for e in requests_of(session)] == [("question", "req-1")]


async def test_a_doubt_answer_needs_the_doubt_asked_now(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    answer = {
        "kind": "doubt_answer",
        "summary": "Responder: la primera",
        "segment_ids": ["seg-1"],
        "pending_id": "p-7",
        "answer": "1",
    }
    detector, _ = context_detector(bus, fake, clock, CONTEXT)
    detector.start()
    try:
        fake.reply_tool(TOOL_NAME, report(answer)).reply_tool(TOOL_NAME, report())
        await segment(bus, session, 1, "la primera")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    assert "no doubt is asked in the chat now" in user_text(fake.requests[1])
    assert requests_of(session) == []


async def test_a_doubt_answer_to_the_asked_doubt_is_published(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    context = CONTEXT.model_copy(update={"doubt": ASKED})
    detector, _ = context_detector(bus, fake, clock, context)
    detector.start()
    try:
        fake.reply_tool(
            TOOL_NAME,
            report(
                {
                    "kind": "doubt_answer",
                    "summary": "Responder «escrita»",
                    "segment_ids": ["seg-1"],
                    "pending_id": "",
                    "answer": " escrita ",
                    "targets": [PAGE_ID.format(n=3)],
                }
            ),
        )
        await segment(bus, session, 1, "pone escrita")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    text = user_text(fake.requests[0])
    assert "Doubt asked in the chat now: p-7 «¿Qué pone?»" in text
    assert "suggestion 2: escrito" in text
    [event] = requests_of(session)
    assert (event["kind"], event["pending_id"], event["answer"]) == (
        "doubt_answer",
        "p-7",
        "escrita",
    )
    assert "targets" not in event


async def test_the_re_asks_follow_structured_reasks(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    bad = {"kind": "edit", "summary": "", "segment_ids": ["seg-1"]}
    detector, _ = context_detector(bus, fake, clock, CONTEXT, llm=LlmSettings(structured_reasks=2))
    detector.start()
    try:
        fake.reply_tool(TOOL_NAME, report(bad)).reply_tool(TOOL_NAME, report(bad))
        fake.reply_tool(
            TOOL_NAME, report({"kind": "edit", "summary": "Tabla", "segment_ids": ["seg-1"]})
        )
        await segment(bus, session, 1, "haz una tabla")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    assert len(fake.requests) == 3
    assert [e["summary"] for e in requests_of(session)] == ["Tabla"]


async def test_typed_requests_do_not_move_the_spoken_numbering(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    for request_id in ("req-1", "req-t1", "req-t2"):
        await bus.publish(
            session.id,
            ASSISTANT_REQUEST_KIND,
            "user" if "t" in request_id[4:] else "observer",
            {
                "request_id": request_id,
                "kind": "edit",
                "summary": "Antes",
                "text": "antes",
                "t_start_ms": 0,
                "t_end_ms": 0,
                **(
                    {"detector": "typed"}
                    if "t" in request_id[4:]
                    else {"detector": "observer", "segment_ids": ["seg-0"]}
                ),
            },
        )
    detector = make_detector(bus, fake, clock)
    detector.start()
    try:
        fake.reply_tool(
            TOOL_NAME, report({"kind": "edit", "summary": "Otra", "segment_ids": ["seg-1"]})
        )
        await segment(bus, session, 1, "otra cosa")
        await advance(detector, clock, session, 2)
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)
    assert [e["request_id"] for e in requests_of(session)][-1] == "req-2"


def test_the_new_kinds_and_fields_of_the_model() -> None:
    old = {
        "request_id": "req-3",
        "kind": "edit",
        "summary": "Hacer una tabla",
        "text": "haz una tabla",
        "segment_ids": ["s-1"],
        "t_start_ms": 10,
        "t_end_ms": 20,
        "detector": "observer",
    }
    # An event written before #327 reads unchanged and dumps back the same.
    assert AssistantRequest.model_validate(old).payload() == old
    typed = {
        "request_id": "req-t1",
        "kind": "set_aside",
        "summary": "Apartar la página 9",
        "text": "aparta la 9",
        "t_start_ms": 0,
        "t_end_ms": 0,
        "detector": "typed",
        "targets": ["sources/notes/page-009.jpg"],
        "message_id": "msg-0123456789abcdef",
    }
    assert AssistantRequest.model_validate(typed).payload() == typed
    for change in (
        {"targets": []},
        {"detector": "observer"},  # a spoken request needs segments
        {"targets": ["a", "a"]},
        {"request_id": "req-x1"},
    ):
        with pytest.raises(ValidationError):
            AssistantRequest.model_validate(typed | change)
    answer = old | {"kind": "doubt_answer", "pending_id": "p-1", "answer": "2"}
    assert AssistantRequest.model_validate(answer).answer == "2"
    with pytest.raises(ValidationError):
        AssistantRequest.model_validate(old | {"kind": "doubt_answer", "answer": "2"})
    assert set(REQUEST_KINDS) >= {"incorporate", "set_aside", "restore", "doubt_answer"}


async def test_a_typed_message_is_classified_with_the_same_prompt_and_tool(
    tmp_vault: Vault, topic: tuple[str, str], fake: FakeClaude
) -> None:
    async def lookup(subject: str, topic_slug: str) -> RequestContext:
        return CONTEXT

    settings = Settings(llm=LlmSettings(), observer=ObserverSettings())
    classifier = MessageClassifier(default_client_factory(settings, fake), sources_lookup=lookup)
    fake.reply_tool(
        TOOL_NAME,
        report(
            {
                "kind": "set_aside",
                "summary": "Apartar la 3",
                "segment_ids": [],
                "targets": [PAGE_ID.format(n=3)],
            },
            {
                "kind": "incorporate",
                "summary": "Incorporar la 4",
                "segment_ids": ["m1"],
                "targets": [PAGE_ID.format(n=4)],
            },
        ),
    )
    found = await asyncio.wait_for(
        classifier.classify(tmp_vault, *topic, "aparta la 3 e incorpora la 4"), WAIT
    )
    assert [(r.kind, r.targets) for r in found] == [
        ("set_aside", [PAGE_ID.format(n=3)]),
        ("incorporate", [PAGE_ID.format(n=4)]),
    ]
    request = fake.requests[0]
    assert request.role == "observer" and request.tools[0]["name"] == TOOL_NAME
    assert "m1 aparta la 3 e incorpora la 4" in user_text(request)
    records = read_conversation(tmp_vault, *topic, MESSAGES_CONVERSATION)
    assert [record.kind for record in records] == ["user", "assistant"]

    fake.fail(LLMAPIError("boom"))
    with pytest.raises(ClassificationError):
        await asyncio.wait_for(classifier.classify(tmp_vault, *topic, "hola"), WAIT)


# -- "quiero estudiar" (#335) ----------------------------------------------------------------------


def test_the_prompt_teaches_the_study_kind() -> None:
    text = load_prompt("observer_requests").content
    assert "`study`" in text
    for phrase in ("ya está, quiero estudiar", "vamos a estudiar esto", "pasa a estudiar"):
        assert phrase in text
    assert "study" in REQUEST_KINDS


async def test_a_spoken_study_request_is_published(
    detector: RequestDetector,
    bus: SessionBus,
    session: Session,
    fake: FakeClaude,
    clock: FakeClock,
) -> None:
    fake.reply_tool(
        TOOL_NAME,
        report(
            {
                "kind": "study",
                "summary": "Pasar a estudiar el tema",
                "segment_ids": ["seg-2"],
                # Fields of other kinds are dropped, not refused.
                "targets": [PAGE_ID.format(n=3)],
            }
        ),
    )
    await segment(bus, session, 1, DICTATION[0])
    await segment(bus, session, 2, "Vale, ya está, quiero estudiar.")
    await advance(detector, clock, session, 2)

    [event] = requests_of(session)
    assert event["kind"] == "study" and event["segment_ids"] == ["seg-2"]
    assert event["text"] == "Vale, ya está, quiero estudiar."
    assert "targets" not in event
    assert len(fake.requests) == 1  # accepted at once, no re-ask


async def test_a_typed_study_message_is_classified(
    tmp_vault: Vault, topic: tuple[str, str], fake: FakeClaude
) -> None:
    settings = Settings(llm=LlmSettings(), observer=ObserverSettings())
    classifier = MessageClassifier(default_client_factory(settings, fake))
    fake.reply_tool(
        TOOL_NAME,
        report({"kind": "study", "summary": "Pasar a estudiar", "segment_ids": ["m1"]}),
    )
    [found] = await asyncio.wait_for(
        classifier.classify(tmp_vault, *topic, "vamos a estudiar esto"), WAIT
    )
    assert found.kind == "study" and found.targets == []


# -- a backend restart (#408) --------------------------------------------------------------------


async def resume(bus: SessionBus, session: Session) -> None:
    await bus.publish(session.id, "session.resumed", "user", {"device_id": None})


async def test_finals_nobody_examined_before_a_restart_are_examined_once(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    first = make_detector(bus, fake, clock)
    first.start()
    try:
        fake.reply_tool(TOOL_NAME, report())
        for n, text in enumerate(DICTATION, start=1):
            await segment(bus, session, n, text)
        await advance(first, clock, session, 2)
        assert len(fake.requests) == 1
        # The request is spoken, and the backend stops before the debounce ends.
        await segment(bus, session, 3, REQUEST)
        await asyncio.wait_for(first.drain(), WAIT)
    finally:
        await asyncio.wait_for(first.stop(), WAIT)
    assert len(fake.requests) == 1

    second = make_detector(bus, fake, clock)
    second.start()
    try:
        fake.reply_tool(
            TOOL_NAME,
            report(
                {
                    "kind": "edit",
                    "summary": "Poner lo último como definición",
                    "segment_ids": ["seg-3"],
                }
            ),
        )
        await resume(bus, session)
        await advance(second, clock, session, 2)
        assert len(fake.requests) == 2
        text = user_text(fake.requests[1])
        assert "seg-1 [3.0s-5.5s] seen" in text and "seg-2 [6.0s-8.5s] seen" in text
        assert f"seg-3 [9.0s-11.5s] new {REQUEST}" in text
        assert [(e["request_id"], e["segment_ids"]) for e in requests_of(session)] == [
            ("req-1", ["seg-3"])
        ]
        await advance(second, clock, session, 20)
        assert len(fake.requests) == 2
    finally:
        await asyncio.wait_for(second.stop(), WAIT)

    # Once examined, a further restart examines nothing again and never repeats the request.
    third = make_detector(bus, fake, clock)
    third.start()
    try:
        await resume(bus, session)
        await advance(third, clock, session, 20)
        assert len(fake.requests) == 2
        assert len(requests_of(session)) == 1
    finally:
        await asyncio.wait_for(third.stop(), WAIT)


async def test_a_conversation_without_examined_ids_is_read_from_its_windows(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    # A conversation written before #408: the user turn has only the rendered window.
    for n, text in enumerate([*DICTATION, REQUEST], start=1):
        await segment(bus, session, n, text)
    append_conversation_record(
        session.vault,
        session.subject_slug,
        session.topic_slug,
        f"observer-requests-{session.id}",
        ConversationRecord(
            time=datetime(2026, 9, 26, 9, 0, tzinfo=UTC),
            kind="user",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Window 1: the newest 2 final segments, 2 new.\n"
                        f"seg-1 [3.0s-5.5s] new {DICTATION[0]}\n"
                        f"seg-2 [6.0s-8.5s] new {DICTATION[1]}",
                    }
                ],
            },
        ),
    )
    detector = make_detector(bus, fake, clock)
    detector.start()
    try:
        fake.reply_tool(TOOL_NAME, report())
        await resume(bus, session)
        await advance(detector, clock, session, 2)
        [request] = fake.requests
        text = user_text(request)
        assert "seg-1 [3.0s-5.5s] seen" in text and "seg-3 [9.0s-11.5s] new" in text
    finally:
        await asyncio.wait_for(detector.stop(), WAIT)


class HangingClaude(FakeClaude):
    """A transport whose calls never answer (a hung Claude), until cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.called = asyncio.Event()
        self.cancelled = 0

    async def send(self, request: LLMRequest, on_text: Any = None) -> Any:
        self.called.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")  # pragma: no cover


async def test_stop_waits_for_a_hung_detection_only_a_bounded_time(
    bus: SessionBus, session: Session, clock: FakeClock
) -> None:
    hanging = HangingClaude()
    settings = Settings(observer=ObserverSettings(stop_timeout_seconds=0.2))
    detector = make_detector(bus, hanging, clock, settings)
    detector.start()
    await segment(bus, session, 1, REQUEST)
    await asyncio.wait_for(detector.drain(), WAIT)
    clock.advance(2)
    await asyncio.wait_for(hanging.called.wait(), WAIT)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(detector.stop(), WAIT)

    assert asyncio.get_running_loop().time() - started < 2.0
    assert hanging.cancelled == 1
    assert not detector.running
