"""`PageTranscriber` on a real `SessionBus` and `tmp_vault`, with `FakeClaude` as the transcriber.

Every wait is bounded (`asyncio.wait_for`), so a job that never ends fails instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from capture_images import desk_still, encode

from studentassistant.config import LlmSettings, Settings, SourcesSettings
from studentassistant.llm import FakeClaude, LLMAPIError, LLMRequest, LLMResponse
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    load_observer_snapshot,
)
from studentassistant.observer.context import PAGE_TRANSCRIPTION_KIND
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources import BurstStill, store_capture
from studentassistant.sources.transcriber import (
    PAGE_TRANSCRIBED_KIND,
    PAGE_TRANSCRIPTION_FAILED_KIND,
    PageTranscriber,
    default_client_factory,
)
from studentassistant.vault import (
    Session,
    Vault,
    create_subject,
    create_topic,
    read_conversation,
    read_ledger,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 10.0
# The window closes at the capture, so a job starts at once (unless a test widens it).
FAST = SourcesSettings(
    capture_window_before_seconds=20,
    capture_window_after_seconds=0,
    transcription_grace_seconds=0,
    transcription_retry_seconds=0,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología").slug
    return subject, create_topic(tmp_vault, subject, "La célula").slug


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


def _transcriber(
    bus: SessionBus,
    fake: Any,
    sources: SourcesSettings = FAST,
    settings: Settings | None = None,
) -> PageTranscriber:
    return PageTranscriber(
        bus,
        bus.attached,
        settings=sources,
        client_factory=default_client_factory(settings or Settings(), fake),
    )


@pytest.fixture
async def transcriber(bus: SessionBus, fake: FakeClaude) -> AsyncIterator[PageTranscriber]:
    worker = _transcriber(bus, fake)
    worker.start()
    yield worker
    await asyncio.wait_for(worker.stop(), WAIT)


async def capture(
    bus: SessionBus,
    session: Session,
    capture_id: str,
    *,
    t_ms: int = 30_000,
    sources: SourcesSettings = FAST,
) -> str:
    stored = await asyncio.to_thread(
        store_capture,
        session.vault,
        session.subject_slug,
        session.topic_slug,
        "notes",
        [BurstStill(encode(desk_still()), "image/jpeg")],
        {"capture_id": capture_id, "source_context": "notes"},
        t_ms,
        sources,
    )
    root = session.vault.path
    source_path = stored.path.relative_to(root).as_posix()
    await bus.publish(
        session.id,
        CAPTURE_EVENT_KIND,
        "phone",
        {
            "capture_id": capture_id,
            "trigger": "button",
            "image_count": 1,
            "source_path": source_path,
            "page_path": stored.page_path.relative_to(root).as_posix(),
            "source_context": "notes",
        },
        t=t_ms,
    )
    return source_path


async def idle(worker: PageTranscriber, session: Session) -> None:
    await asyncio.wait_for(worker.wait_idle(session.id), WAIT)


def events(session: Session, kind: str | None = None) -> list[Any]:
    return [e for e in session.read_events() if kind is None or e.kind == kind]


def request_text(request: LLMRequest) -> str:
    return request.messages[0]["content"][-1]["text"]


# -- the happy path --------------------------------------------------------------------------------


async def test_a_stored_page_is_transcribed_with_pending_items_and_an_event(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    assert PAGE_TRANSCRIBED_KIND == PAGE_TRANSCRIPTION_KIND  # what the observer reads
    fake.reply_text("# La célula\n\n- La [[?mitocondria]] produce [[?]].")
    await bus.publish(
        session.id,
        "transcript.final",
        "stt",
        {"segment_id": "s1", "session_start_ms": 27_000, "session_end_ms": 29_000, "text": "mito"},
        t=29_000,
    )
    source_path = await capture(bus, session, "cap-1")
    await idle(transcriber, session)

    [request] = fake.requests
    assert request.role == "transcriber"
    blocks = request.messages[0]["content"]
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert "- [27.0-29.0 s] mito" in request_text(request)

    md = source_path.replace(".jpg", ".md")
    assert (session.vault.path / md).read_text(encoding="utf-8").startswith("# La célula")

    kinds = [e.kind for e in events(session)]
    assert kinds[-3:] == [STATE_OP_EVENT_KIND, STATE_OP_EVENT_KIND, PAGE_TRANSCRIBED_KIND]
    [done] = events(session, PAGE_TRANSCRIBED_KIND)
    assert done.origin == "observer"
    assert done.payload["capture_id"] == "cap-1"
    assert done.payload["text"] == "# La célula\n\n- La [[?mitocondria]] produce [[?]]."
    assert done.payload["path"] == md
    assert done.payload["uncertain"] == 2
    assert done.payload["hint_segments"] == 1
    assert done.payload["attempts"] == 1
    ops = [e.payload for e in events(session, STATE_OP_EVENT_KIND)]
    assert [op["pending_id"] for op in ops] == done.payload["pending_ids"]
    assert all(op["op"] == "add_pending" and op["category"] == "illegible" for op in ops)
    assert all(op["capture_ids"] == ["cap-1"] for op in ops)

    # The observer's fold takes the pending items, each tied to the page.
    state = load_observer_snapshot(session.vault, *topic_of(session), write_back=False).state
    assert [(p.id, p.capture_ids) for p in state.open_pending()] == [
        (op["pending_id"], ["cap-1"]) for op in ops
    ]

    [entry] = read_ledger(session.vault, *topic_of(session))
    assert entry.role == "transcriber" and entry.session == session.id
    records = read_conversation(session.vault, *topic_of(session), f"transcriber-{session.id}")
    assert [r.kind for r in records] == ["user", "assistant"]
    image = records[0].message["content"][0]  # type: ignore[index]
    assert image == {
        "type": "image",
        "source": {"type": "vault", "path": done.payload["page_path"]},
    }


def topic_of(session: Session) -> tuple[str, str]:
    return session.subject_slug, session.topic_slug


async def test_a_page_without_marks_adds_no_pending_item(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("# Limpio")
    await capture(bus, session, "cap-1")
    await idle(transcriber, session)
    assert events(session, STATE_OP_EVENT_KIND) == []
    [done] = events(session, PAGE_TRANSCRIBED_KIND)
    assert done.payload["pending_ids"] == []


async def test_a_repeated_capture_event_is_transcribed_once(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("# Uno")
    source_path = await capture(bus, session, "cap-1")
    await bus.publish(
        session.id,
        CAPTURE_EVENT_KIND,
        "phone",
        {"capture_id": "cap-1", "source_path": source_path},
    )
    await idle(transcriber, session)
    assert len(fake.requests) == 1
    assert len(events(session, PAGE_TRANSCRIBED_KIND)) == 1


# -- timing ----------------------------------------------------------------------------------------


async def test_a_page_waits_for_its_window_and_the_flush_cuts_the_wait_short(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    slow = FAST.model_copy(update={"capture_window_after_seconds": 600})
    worker = _transcriber(bus, fake, slow)
    worker.start()
    try:
        fake.reply_text("# Tarde")
        await capture(bus, session, "cap-1", sources=slow)
        await asyncio.sleep(0.05)
        assert fake.requests == []  # still waiting for the 10 minutes after the photo
        await bus.publish(
            session.id,
            "transcript.final",
            "stt",
            {
                "segment_id": "s1",
                "session_start_ms": 40_000,
                "session_end_ms": 41_000,
                "text": "dicho después de la foto",
            },
        )
        await asyncio.wait_for(worker.flush(session.id), WAIT)
        assert len(fake.requests) == 1
        assert "dicho después de la foto" in request_text(fake.requests[0])
        assert len(events(session, PAGE_TRANSCRIBED_KIND)) == 1
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)


class GatedClaude(FakeClaude):
    """A fake whose calls wait for `gate`, counting how many are in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.in_flight = 0
        self.most = 0

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await self.gate.wait()
            return await super().send(request)
        finally:
            self.in_flight -= 1


async def test_at_most_the_configured_number_of_pages_go_to_claude_at_once(
    bus: SessionBus, session: Session
) -> None:
    gated = GatedClaude()
    for n in range(3):
        gated.reply_text(f"# Página {n}")
    worker = _transcriber(bus, gated, FAST.model_copy(update={"transcription_concurrency": 2}))
    worker.start()
    try:
        for n in range(3):
            await capture(bus, session, f"cap-{n}")
        for _ in range(50):
            if gated.in_flight == 2:
                break
            await asyncio.sleep(0.01)
        assert gated.in_flight == 2
        gated.gate.set()
        await idle(worker, session)
        assert gated.most == 2
        assert len(events(session, PAGE_TRANSCRIBED_KIND)) == 3
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)


# -- failures --------------------------------------------------------------------------------------


async def test_a_failed_attempt_is_retried(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.fail(LLMAPIError("bad gateway", status_code=400)).reply_text("   ").reply_text("# Bien")
    await capture(bus, session, "cap-1")
    await idle(transcriber, session)
    assert len(fake.requests) == 3
    [done] = events(session, PAGE_TRANSCRIBED_KIND)
    assert done.payload["attempts"] == 3
    assert events(session, PAGE_TRANSCRIPTION_FAILED_KIND) == []


async def test_a_page_that_keeps_failing_is_reported_and_writes_nothing(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    for _ in range(3):
        fake.fail(LLMAPIError("no", status_code=400))
    fake.reply_text("# Otra")  # the next page still works
    source_path = await capture(bus, session, "cap-1")
    await idle(transcriber, session)
    [failed] = events(session, PAGE_TRANSCRIPTION_FAILED_KIND)
    assert failed.payload["capture_id"] == "cap-1"
    assert failed.payload["reason"] == "error"
    assert failed.payload["attempts"] == 3
    assert not (session.vault.path / source_path.replace(".jpg", ".md")).exists()
    assert events(session, PAGE_TRANSCRIBED_KIND) == []

    await capture(bus, session, "cap-2")
    await idle(transcriber, session)
    assert [e.payload["capture_id"] for e in events(session, PAGE_TRANSCRIBED_KIND)] == ["cap-2"]


async def test_a_refusal_is_not_retried(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("no", stop_reason="refusal")
    await capture(bus, session, "cap-1")
    await idle(transcriber, session)
    assert len(fake.requests) == 1
    [failed] = events(session, PAGE_TRANSCRIPTION_FAILED_KIND)
    assert failed.payload["reason"] == "refused"


async def test_a_reached_cost_cap_sends_nothing_and_is_reported(
    bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    capped = Settings(llm=LlmSettings(max_usd_per_session=0))
    worker = _transcriber(bus, fake, FAST, capped)
    worker.start()
    try:
        fake.reply_text("# Nunca")
        await capture(bus, session, "cap-1")
        await idle(worker, session)
        assert fake.requests == []
        [failed] = events(session, PAGE_TRANSCRIPTION_FAILED_KIND)
        assert failed.payload["reason"] == "cost_cap"
        assert failed.payload["attempts"] == 1
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)


async def test_a_job_whose_session_ended_never_breaks_the_transcriber(
    transcriber: PageTranscriber, bus: SessionBus, session: Session, fake: FakeClaude
) -> None:
    fake.reply_text("# Uno")
    await capture(bus, session, "cap-1")
    await asyncio.wait_for(transcriber.drain(), WAIT)
    bus.detach(session.id)  # the session ended before the answer could be published
    await idle(transcriber, session)
    assert len(fake.requests) == 1
    assert events(session, PAGE_TRANSCRIBED_KIND) == []
    assert transcriber.running
