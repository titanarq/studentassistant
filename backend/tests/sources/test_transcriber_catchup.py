"""Catch-up of the page transcriber (#181): owed pages after a restart or a session-end timeout.

A real `SessionBus` and `tmp_vault`, `FakeClaude` as the transcriber. Every wait is bounded.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from capture_images import desk_still, encode

from studentassistant.config import Settings, SourcesSettings
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.observer import (
    CAPTURE_EVENT_KIND,
    STATE_OP_EVENT_KIND,
    EventRef,
    load_observer_snapshot,
    op_payload,
)
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.bus import SessionBus
from studentassistant.sources import BurstStill, store_capture
from studentassistant.sources.catchup import owed_pages, transcription_path
from studentassistant.sources.transcriber import (
    CAPTURE_SESSION_KEY,
    PAGE_TRANSCRIBED_KIND,
    PageTranscriber,
    default_client_factory,
)
from studentassistant.sources.transcription import find_uncertain, pending_ops
from studentassistant.vault import (
    Event,
    Session,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_page_transcription,
    resume_session,
    start_session,
)

pytestmark = pytest.mark.anyio

WAIT = 10.0
FAST = SourcesSettings(
    capture_window_before_seconds=20,
    capture_window_after_seconds=0,
    transcription_grace_seconds=0,
    transcription_retry_seconds=0,
)
MARKED = "# La célula\n\n- La [[?mitocondria]] produce energía."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Biología").slug
    return subject, create_topic(tmp_vault, subject, "La célula").slug


def _transcriber(bus: SessionBus, fake: Any, **options: Any) -> PageTranscriber:
    return PageTranscriber(
        bus,
        bus.attached,
        settings=FAST,
        client_factory=default_client_factory(Settings(), fake),
        **options,
    )


async def capture(bus: SessionBus, session: Session, capture_id: str) -> str:
    stored = await asyncio.to_thread(
        store_capture,
        session.vault,
        session.subject_slug,
        session.topic_slug,
        "notes",
        [BurstStill(encode(desk_still()), "image/jpeg")],
        {"capture_id": capture_id, "source_context": "notes"},
        30_000,
        FAST,
    )
    root = session.vault.path
    source_path = stored.path.relative_to(root).as_posix()
    await bus.publish(
        session.id,
        CAPTURE_EVENT_KIND,
        "phone",
        {
            "capture_id": capture_id,
            "source_path": source_path,
            "page_path": stored.page_path.relative_to(root).as_posix(),
            "source_context": "notes",
        },
        t=30_000,
    )
    return source_path


async def open_session(bus: SessionBus, session: Session, kind: str = "session.started") -> None:
    bus.attach(session)
    await bus.publish(session.id, kind, "user", {})


def end(bus: SessionBus, session: Session) -> None:
    end_session(session)
    bus.detach(session.id)


def events(session: Session, kind: str | None = None) -> list[Any]:
    return [e for e in session.read_events() if kind is None or e.kind == kind]


def pending_of(vault: Vault, topic: tuple[str, str]) -> list[str]:
    state = load_observer_snapshot(vault, *topic, write_back=False).state
    return [item.id for item in state.open_pending()]


class GatedClaude(FakeClaude):
    """A fake whose calls wait for `gate` (a call outliving the session's end hook)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.called = asyncio.Event()

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.called.set()
        await self.gate.wait()
        return await super().send(request)


# -- the pure part ---------------------------------------------------------------------------------


def _event(seq: int, kind: str, **payload: Any) -> Event:
    return Event(seq=seq, t=seq, kind=kind, origin="phone", payload=payload)


def test_owed_pages_are_the_captures_without_a_recorded_transcription() -> None:
    cap = {"source_path": "subjects/b/topics/t/sources/notes/page-001.jpg"}
    log = [
        ("20260101-000000", _event(1, CAPTURE_EVENT_KIND, capture_id="a", **cap)),
        ("20260101-000000", _event(2, CAPTURE_EVENT_KIND, capture_id="b", **cap)),
        ("20260101-000000", _event(3, CAPTURE_EVENT_KIND, capture_id="c", **cap)),
        ("20260101-000000", _event(4, "page.transcribed", capture_id="a")),
        ("20260101-000000", _event(5, "page.transcription_failed", capture_id="c", reason="error")),
        ("20260102-000000", _event(1, CAPTURE_EVENT_KIND, capture_id="a", **cap)),
        ("20260102-000000", _event(2, CAPTURE_EVENT_KIND, capture_id="d", **cap)),
        ("20260102-000000", _event(3, CAPTURE_EVENT_KIND, capture_id="r", **cap)),
        # b's transcription recorded later, in another session of the topic
        (
            "20260102-000000",
            _event(
                4, "page.transcribed", capture_id="b", **{CAPTURE_SESSION_KEY: "20260101-000000"}
            ),
        ),
        (
            "20260102-000000",
            _event(5, "page.transcription_failed", capture_id="r", reason="refused"),
        ),
        (
            "20260102-000000",
            _event(6, STATE_OP_EVENT_KIND, op="add_pending", pending_id="ill-x-1"),
        ),
        ("20260102-000000", _event(7, "session.resumed")),
        ("20260102-000000", _event(8, CAPTURE_EVENT_KIND, capture_id="late", **cap)),
    ]
    owed, pending_ids = owed_pages(log, before=EventRef(session_id="20260102-000000", seq=7))
    assert [(p.session_id, p.capture_id) for p in owed] == [
        ("20260101-000000", "c"),  # failed with an error: tried again
        ("20260102-000000", "a"),  # the same id in another session is another page
        ("20260102-000000", "d"),
    ]
    assert pending_ids == {"ill-x-1"}
    only, _ = owed_pages(log, sessions={"20260101-000000"})
    assert [p.capture_id for p in only] == ["c"]
    assert transcription_path(cap["source_path"]).endswith("sources/notes/page-001.md")


# -- a restart -------------------------------------------------------------------------------------


async def test_a_resumed_session_transcribes_the_pages_a_restart_cut_short(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    # The first run stored a page and stopped before any transcriber answered.
    first = SessionBus()
    session = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    first.attach(session)
    source_path = await capture(first, session, "cap-1")

    # The restart: a new bus, the session resumed, a fresh transcriber.
    bus = SessionBus()
    fake = FakeClaude().reply_text(MARKED)
    worker = _transcriber(bus, fake)
    worker.start()
    try:
        resumed = resume_session(tmp_vault, *topic)
        await open_session(bus, resumed, "session.resumed")
        await asyncio.wait_for(worker.wait_idle(resumed.id), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)

    [request] = fake.requests
    assert request.role == "transcriber"
    assert (tmp_vault.path / transcription_path(source_path)).is_file()
    [done] = events(resumed, PAGE_TRANSCRIBED_KIND)
    assert done.payload["capture_id"] == "cap-1"
    assert done.payload[CAPTURE_SESSION_KEY] == session.id
    assert done.payload["recovered"] is False
    assert pending_of(tmp_vault, topic) == done.payload["pending_ids"] != []


async def test_server_start_transcribes_and_the_next_session_records_the_events(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    # An ended session whose page was never transcribed (the backend stopped mid-call).
    first = SessionBus()
    old = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    first.attach(old)
    source_path = await capture(first, old, "cap-1")
    end(first, old)

    bus = SessionBus()
    fake = FakeClaude().reply_text(MARKED)
    worker = _transcriber(bus, fake)
    worker.start()
    try:
        worker.catch_up_vault(tmp_vault)  # the server opened the vault
        await asyncio.wait_for(worker.wait_startup(), WAIT)
        await asyncio.wait_for(worker.wait_idle(old.id), WAIT)
        assert len(fake.requests) == 1
        assert (tmp_vault.path / transcription_path(source_path)).is_file()
        assert events(old, PAGE_TRANSCRIBED_KIND) == []  # nothing attached: the events wait

        new = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, new)
        await asyncio.wait_for(worker.wait_idle(new.id), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)

    assert len(fake.requests) == 1  # recorded from page-001.md, not transcribed again
    [done] = events(new, PAGE_TRANSCRIBED_KIND)
    assert done.payload[CAPTURE_SESSION_KEY] == old.id
    assert done.payload["recovered"] is True
    assert done.payload["text"] == MARKED
    ops = events(new, STATE_OP_EVENT_KIND)
    assert [op.payload["pending_id"] for op in ops] == done.payload["pending_ids"]
    assert pending_of(tmp_vault, topic) == done.payload["pending_ids"]


# -- a session-end timeout -------------------------------------------------------------------------


async def test_a_page_finished_after_its_session_ended_is_recorded_in_the_next_session(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    bus = SessionBus()
    gated = GatedClaude()
    gated.reply_text(MARKED)
    worker = _transcriber(bus, gated)
    worker.start()
    try:
        old = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, old)
        source_path = await capture(bus, old, "cap-1")
        await asyncio.wait_for(gated.called.wait(), WAIT)
        # The end hook timed out: the session ends with the call still in flight.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.flush(old.id), 0.05)
        await bus.publish(old.id, "session.ended", "user", {})
        await asyncio.wait_for(worker.drain(), WAIT)
        end(bus, old)
        gated.gate.set()
        await asyncio.wait_for(worker.wait_idle(old.id), WAIT)
        assert (tmp_vault.path / transcription_path(source_path)).is_file()
        assert events(old, PAGE_TRANSCRIBED_KIND) == []

        new = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, new)
        await asyncio.wait_for(worker.wait_idle(new.id), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)

    assert len(gated.requests) == 1
    [done] = events(new, PAGE_TRANSCRIBED_KIND)
    assert (done.payload["capture_id"], done.payload[CAPTURE_SESSION_KEY]) == ("cap-1", old.id)
    assert done.payload["recovered"] is True
    assert pending_of(tmp_vault, topic) == done.payload["pending_ids"] != []


async def test_a_late_page_goes_to_the_live_session_of_its_topic(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    bus = SessionBus()
    gated = GatedClaude()
    gated.reply_text(MARKED)
    worker = _transcriber(bus, gated)
    worker.start()
    try:
        old = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, old)
        await capture(bus, old, "cap-1")
        await asyncio.wait_for(gated.called.wait(), WAIT)
        await bus.publish(old.id, "session.ended", "user", {})
        await asyncio.wait_for(worker.drain(), WAIT)
        end(bus, old)
        # The next session starts while the call is still in flight: its catch-up skips the page.
        new = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, new)
        await asyncio.wait_for(worker.drain(), WAIT)
        gated.gate.set()
        await asyncio.wait_for(worker.wait_idle(old.id), WAIT)
        await asyncio.wait_for(worker.wait_idle(new.id), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)

    assert len(gated.requests) == 1
    [done] = events(new, PAGE_TRANSCRIBED_KIND)
    assert done.payload[CAPTURE_SESSION_KEY] == old.id
    assert done.payload["recovered"] is False
    assert pending_of(tmp_vault, topic) == done.payload["pending_ids"]


async def test_pending_items_already_in_the_log_are_not_added_twice(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    # The page was written and its pending items published, then the session ended before
    # `page.transcribed` (the stop came between the two).
    bus = SessionBus()
    old = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    bus.attach(old)
    source_path = await capture(bus, old, "cap-1")
    put_page_transcription(tmp_vault, source_path, MARKED + "\n")
    ops = pending_ops(
        find_uncertain(MARKED),
        session_id=old.id,
        capture_id="cap-1",
        source_kind="notes",
        page_number=1,
    )
    for op in ops:
        await bus.publish(old.id, STATE_OP_EVENT_KIND, "observer", op_payload(op))
    end(bus, old)

    fake = FakeClaude()
    worker = _transcriber(bus, fake)
    worker.start()
    try:
        new = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
        await open_session(bus, new)
        await asyncio.wait_for(worker.wait_idle(new.id), WAIT)
    finally:
        await asyncio.wait_for(worker.stop(), WAIT)

    assert fake.requests == []
    assert events(new, STATE_OP_EVENT_KIND) == []
    [done] = events(new, PAGE_TRANSCRIBED_KIND)
    assert done.payload["pending_ids"] == [op.pending_id for op in ops]
    assert pending_of(tmp_vault, topic) == [op.pending_id for op in ops]  # the fold still loads


async def test_the_startup_catch_up_skips_review_sessions(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    # A review session (a doubt's resolution, #191) holds no captures: the newest session whose
    # pages are owed is still the study session before it.
    from studentassistant.sources.transcriber import _startup_plan

    study = start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION)
    end_session(study)
    end_session(start_session(tmp_vault, *topic, "pc", PROTOCOL_VERSION, kind="review"))

    assert _startup_plan(tmp_vault) == [(*topic, [study.id])]
