"""The session lifecycle service: subjects/topics, start/resume/end, one active session, sync."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from studentassistant.config import VaultSettings
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.server.auth import Principal
from studentassistant.server.bus import SessionBus
from studentassistant.server.sessions import (
    SESSION_ENDED,
    SESSION_RESUMED,
    SESSION_STARTED,
    ActiveSessionExistsError,
    SessionAlreadyEndedError,
    SessionService,
    UnknownSessionError,
    VaultUnavailableError,
)
from studentassistant.vault import (
    Event,
    GitSync,
    NoOpenSessionError,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    read_jsonl,
    resume_session,
    start_session,
)
from studentassistant.vault import sessions as vault_sessions

pytestmark = pytest.mark.anyio

DEVICE = Principal(device_id="dev-1")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def service(tmp_vault: Vault) -> SessionService:
    return SessionService(SessionBus(), vault=tmp_vault, host="pc-test")


async def _topic(
    service: SessionService, subject: str = "Física", topic: str = "Cinemática"
) -> tuple[str, str]:
    created = await service.create_subject(subject)
    return created.subject_id, (await service.create_topic(created.subject_id, topic)).topic_id


# subjects and topics


async def test_subjects_and_topics_are_listed_and_created_with_slugs_as_ids(
    service: SessionService,
) -> None:
    assert (await service.list_subjects()).subjects == []
    subject = await service.create_subject("Física")
    topic = await service.create_topic(subject.subject_id, "Cinemática")

    assert (subject.subject_id, subject.name) == ("fisica", "Física")
    assert (topic.topic_id, topic.subject_id, topic.name) == ("cinematica", "fisica", "Cinemática")
    assert topic.open_session_id is None
    assert [s.subject_id for s in (await service.list_subjects()).subjects] == ["fisica"]
    listed = await service.list_topics("fisica")
    assert listed.subject_id == "fisica"
    assert [t.topic_id for t in listed.topics] == ["cinematica"]


async def test_an_unknown_subject_or_topic_is_refused(service: SessionService) -> None:
    with pytest.raises(SubjectNotFoundError):
        await service.list_topics("nada")
    with pytest.raises(SubjectNotFoundError):
        await service.create_topic("nada", "Tema")
    subject = await service.create_subject("Física")
    with pytest.raises(TopicNotFoundError):
        await service.start(subject.subject_id, "nada", client_time_ms=1)


# start


async def test_start_opens_a_session_of_one_topic_and_publishes_session_started(
    service: SessionService,
) -> None:
    subject_id, topic_id = await _topic(service)
    with service.bus.subscribe() as subscription:
        session = await service.start(
            subject_id, topic_id, client_time_ms=1_790_000_000_000, principal=DEVICE
        )
        event = subscription.get_nowait()

    assert session.status == "active"
    assert (session.subject_id, session.topic_id) == (subject_id, topic_id)
    assert session.ws_path == f"/ws/sessions/{session.session_id}"
    assert session.protocol_version == PROTOCOL_VERSION
    assert (event.kind, event.seq, event.origin) == (SESSION_STARTED, 1, "user")
    assert event.payload == {
        "subject_id": subject_id,
        "topic_id": topic_id,
        "client_time_ms": 1_790_000_000_000,
        "device_id": "dev-1",
    }
    assert service.active is not None and service.active.session_id == session.session_id
    assert service.get_active(session.session_id) == service.active
    topics = (await service.list_topics(subject_id)).topics
    assert topics[0].open_session_id == session.session_id


async def test_a_second_start_is_rejected_while_a_session_is_active(
    service: SessionService,
) -> None:
    subject_id, topic_id = await _topic(service)
    other = await service.create_topic(subject_id, "Dinámica")
    first = await service.start(subject_id, topic_id, client_time_ms=1)

    with pytest.raises(ActiveSessionExistsError) as refused:
        await service.start(subject_id, other.topic_id, client_time_ms=2)
    assert refused.value.session_id == first.session_id
    assert first.session_id in str(refused.value)


# resume


async def test_resume_after_a_restart_continues_the_seq_of_the_log(tmp_vault: Vault) -> None:
    before = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")
    subject_id, topic_id = await _topic(before)
    started = await before.start(subject_id, topic_id, client_time_ms=1)
    await before.bus.publish(started.session_id, "marker", "phone")
    await before.bus.publish(started.session_id, "marker", "phone")

    # The backend restarts without ending the session.
    after = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")
    topics = (await after.list_topics(subject_id)).topics
    assert topics[0].open_session_id == started.session_id
    assert after.active is None
    with pytest.raises(ActiveSessionExistsError):
        await after.start(subject_id, topic_id, client_time_ms=2)

    with after.bus.subscribe() as subscription:
        resumed = await after.resume(started.session_id, principal=DEVICE)
        event = subscription.get_nowait()

    assert resumed.session_id == started.session_id
    assert resumed.started_at_ms == started.started_at_ms
    assert (event.kind, event.seq, event.payload) == (SESSION_RESUMED, 4, {"device_id": "dev-1"})
    marker = await after.bus.publish(started.session_id, "marker", "phone")
    assert marker.seq == 5


async def test_resuming_the_active_session_again_is_a_reconnect(
    tmp_vault: Vault, service: SessionService
) -> None:
    subject_id, topic_id = await _topic(service)
    started = await service.start(subject_id, topic_id, client_time_ms=1)
    again = await service.resume(started.session_id)
    assert again == started
    events = list(resume_session(tmp_vault, subject_id, topic_id).read_events())
    assert [(e.seq, e.kind) for e in events] == [(1, SESSION_STARTED), (2, SESSION_RESUMED)]


class _Later(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return datetime.now(tz) + timedelta(minutes=1)


async def test_resuming_another_session_while_one_is_active_is_rejected(
    tmp_vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = create_subject(tmp_vault, "Física")
    one = create_topic(tmp_vault, subject.slug, "Cinemática")
    two = create_topic(tmp_vault, subject.slug, "Dinámica")
    # Two sessions left unended by another backend, say.
    first = start_session(tmp_vault, subject.slug, one.slug, "pc", PROTOCOL_VERSION)
    # Session ids are unique per topic only; start the second one a minute later.
    real_datetime = vault_sessions.datetime
    monkeypatch.setattr(vault_sessions, "datetime", _Later)
    second = start_session(tmp_vault, subject.slug, two.slug, "pc", PROTOCOL_VERSION)
    # Not `monkeypatch.undo()`: that would also give `HOME` back, and the service's index would
    # land in the real `~/.cache`.
    monkeypatch.setattr(vault_sessions, "datetime", real_datetime)
    assert first.id != second.id
    service = SessionService(SessionBus(), vault=tmp_vault)

    await service.resume(first.id)
    with pytest.raises(ActiveSessionExistsError) as refused:
        await service.resume(second.id)
    assert refused.value.session_id == first.id


async def test_resume_of_an_unknown_or_ended_session_is_refused(
    service: SessionService,
) -> None:
    subject_id, topic_id = await _topic(service)
    started = await service.start(subject_id, topic_id, client_time_ms=1)
    await service.end(started.session_id, client_time_ms=2, reason="button")

    with pytest.raises(SessionAlreadyEndedError):
        await service.resume(started.session_id)
    with pytest.raises(UnknownSessionError):
        await service.resume("20000101-000000")


# end


async def test_end_publishes_session_ended_then_ends_the_session(
    tmp_vault: Vault, service: SessionService
) -> None:
    subject_id, topic_id = await _topic(service)
    started = await service.start(subject_id, topic_id, client_time_ms=1)
    with service.bus.subscribe() as subscription:
        ended = await service.end(
            started.session_id, client_time_ms=5, reason="command", principal=DEVICE
        )
        event = subscription.get_nowait()

    assert (ended.session_id, ended.status) == (started.session_id, "ended")
    assert ended.ended_at_ms >= started.started_at_ms
    assert (event.kind, event.seq) == (SESSION_ENDED, 2)
    assert event.payload == {"client_time_ms": 5, "reason": "command", "device_id": "dev-1"}
    assert service.active is None
    assert not service.bus.is_attached(started.session_id)
    assert (await service.list_topics(subject_id)).topics[0].open_session_id is None
    # The vault agrees: nothing left to resume, and the log ends with the end.
    with pytest.raises(NoOpenSessionError):
        resume_session(tmp_vault, subject_id, topic_id)
    with pytest.raises(SessionAlreadyEndedError):
        await service.end(started.session_id, client_time_ms=6, reason="button")
    with pytest.raises(UnknownSessionError):
        await service.end("20000101-000000", client_time_ms=6, reason="button")
    # Ending freed the backend for the next session.
    await service.start(subject_id, topic_id, client_time_ms=7)


async def test_a_session_left_unended_by_an_earlier_run_can_be_ended(tmp_vault: Vault) -> None:
    subject = create_subject(tmp_vault, "Física")
    topic = create_topic(tmp_vault, subject.slug, "Cinemática")
    left = start_session(tmp_vault, subject.slug, topic.slug, "pc", PROTOCOL_VERSION)
    left.append_event("marker", "phone")
    service = SessionService(SessionBus(), vault=tmp_vault)

    await service.end(left.id, client_time_ms=1, reason="button")

    events = list(read_jsonl(left.events_path, Event))
    assert [(e.seq, e.kind) for e in events] == [(1, "marker"), (2, SESSION_ENDED)]
    assert not service.bus.is_attached(left.id)


async def test_end_checkpoints_the_vault_and_pushes_it(tmp_vault: Vault, git_origin: Path) -> None:
    sync = GitSync(tmp_vault)
    service = SessionService(SessionBus(), vault=tmp_vault, sync=sync)
    subject_id, topic_id = await _topic(service)
    started = await service.start(subject_id, topic_id, client_time_ms=1)

    await service.end(started.session_id, client_time_ms=2, reason="button")

    def origin(*args: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(git_origin), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    assert origin("log", "-1", "--format=%s", "main").strip() == (
        f"sesión {started.session_id} terminada"
    )
    events_path = (
        f"subjects/{subject_id}/topics/{topic_id}/sessions/{started.session_id}/events.jsonl"
    )
    assert SESSION_ENDED in origin("show", f"main:{events_path}")
    assert sync.status().pending_commits == 0


async def test_a_vault_that_cannot_be_opened_is_reported(tmp_path: Path) -> None:
    service = SessionService(SessionBus(), vault_settings=VaultSettings(path=tmp_path / "none"))
    with pytest.raises(VaultUnavailableError):
        await service.list_subjects()


async def test_the_configured_vault_is_opened_lazily(tmp_vault: Vault) -> None:
    service = SessionService(SessionBus(), vault_settings=VaultSettings(path=tmp_vault.path))
    await service.create_subject("Química")
    assert [s.name for s in (await service.list_subjects()).subjects] == ["Química"]
