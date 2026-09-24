"""`SessionService.end` hooks: order around `session.ended`, failures, and the pipeline drain."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from studentassistant.server.bus import SessionBus
from studentassistant.server.sessions import SESSION_ENDED, SessionService
from studentassistant.stt import TranscriptPipeline
from studentassistant.vault import Event, Session, Vault, read_jsonl

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def service(tmp_vault: Vault) -> SessionService:
    return SessionService(SessionBus(), vault=tmp_vault, host="pc-test", end_hook_timeout=0.5)


async def _start(service: SessionService) -> Session:
    subject = await service.create_subject("Física")
    topic = await service.create_topic(subject.subject_id, "Cinemática")
    started = await service.start(subject.subject_id, topic.topic_id, client_time_ms=1)
    return await service.require_active(started.session_id)


def _kinds(session: Session) -> list[str]:
    return [e.kind for e in read_jsonl(session.events_path, Event)]


async def test_hooks_run_before_and_after_session_ended_while_the_session_is_open(
    service: SessionService,
) -> None:
    session = await _start(service)
    seen: list[tuple[str, str, bool, bool, str]] = []

    def hook(stage: str):  # noqa: ANN202 - a small factory of async hooks
        async def run(session_id: str) -> None:
            seen.append(
                (
                    stage,
                    session_id,
                    service.bus.is_attached(session_id),
                    session.meta.ended_at is None,
                    _kinds(session)[-1],
                )
            )

        return run

    service.add_before_close(hook("close-1"))
    service.add_before_ended(hook("ended-1"))
    service.add_before_ended(hook("ended-2"))
    service.add_before_close(hook("close-2"))

    await service.end(session.id, client_time_ms=5, reason="button")

    assert seen == [
        ("ended-1", session.id, True, True, "session.started"),
        ("ended-2", session.id, True, True, "session.started"),
        ("close-1", session.id, True, True, SESSION_ENDED),
        ("close-2", session.id, True, True, SESSION_ENDED),
    ]
    assert not service.bus.is_attached(session.id)


async def test_a_before_ended_hook_can_still_publish_into_the_session(
    service: SessionService,
) -> None:
    session = await _start(service)

    async def flush_tail(session_id: str) -> None:
        await service.bus.publish(session_id, "transcript.final", "stt", {"text": "cola"})

    service.add_before_ended(flush_tail)
    await service.end(session.id, client_time_ms=5, reason="command")

    assert _kinds(session) == ["session.started", "transcript.final", SESSION_ENDED]


async def test_a_failing_or_hanging_hook_is_logged_and_the_session_ends_anyway(
    service: SessionService, caplog: pytest.LogCaptureFixture
) -> None:
    session = await _start(service)
    ran: list[str] = []

    async def fails(session_id: str) -> None:
        raise RuntimeError("boom")

    async def hangs(session_id: str) -> None:
        await asyncio.Event().wait()

    async def after(session_id: str) -> None:
        ran.append(session_id)

    service.add_before_ended(fails)
    service.add_before_close(hangs)
    service.add_before_close(after)

    with caplog.at_level(logging.ERROR, logger="studentassistant.server.sessions"):
        ended = await asyncio.wait_for(
            service.end(session.id, client_time_ms=5, reason="button"), timeout=5
        )

    assert ended.status == "ended"
    assert ran == [session.id]
    assert session.meta.ended_at is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any("failed" in m for m in messages)
    assert any("timed out" in m for m in messages)


async def test_ending_right_after_finals_waits_for_the_pipeline_to_write_them(
    service: SessionService,
) -> None:
    pipeline = TranscriptPipeline(service.bus, service.bus.attached)
    service.add_before_close(lambda _session_id: pipeline.drain())
    pipeline.start()
    try:
        session = await _start(service)
        # A slow vault write, so the pipeline is still behind when `end` is called.
        append = session.append_transcript

        def slow_append(*args: object, **kwargs: object) -> object:
            time.sleep(0.02)
            return append(*args, **kwargs)  # type: ignore[arg-type]

        session.append_transcript = slow_append  # type: ignore[method-assign]
        texts = [f"frase {n}" for n in range(20)]
        for n, text in enumerate(texts):
            payload = {
                "segment_id": f"s{n}",
                "session_start_ms": 1_000 * n,
                "session_end_ms": 1_000 * n + 800,
                "text": text,
                "language": "es",
                "provider": "web-speech",
            }
            await service.bus.publish(session.id, "transcript.final", "stt", payload)
        # No explicit drain: `end` has to wait for the pipeline itself.
        await asyncio.wait_for(service.end(session.id, client_time_ms=5, reason="button"), 5)
    finally:
        await pipeline.stop()

    assert [s.text for s in session.read_transcript()] == texts
