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
from studentassistant.llm import FakeClaude, LLMRequest
from studentassistant.observer import (
    ASSISTANT_REQUEST_KIND,
    AssistantRequest,
    fold,
)
from studentassistant.observer.live import STATUS_EVENT_KIND, default_client_factory
from studentassistant.observer.requests import TOOL_NAME, RequestDetector
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.vault import (
    Session,
    Vault,
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


async def test_off_and_wake_word_make_no_call(
    bus: SessionBus, session: Session, fake: FakeClaude, clock: FakeClock
) -> None:
    for mode in ("off", "wake_word"):
        settings = Settings(observer=ObserverSettings(request_detection=mode))
        detector = make_detector(bus, fake, clock, settings)
        detector.start()
        assert not detector.running
        await segment(bus, session, 1, REQUEST)
        clock.advance(20)
        await asyncio.sleep(0)
        await asyncio.wait_for(detector.flush(session.id), WAIT)
        await asyncio.wait_for(detector.stop(), WAIT)
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
