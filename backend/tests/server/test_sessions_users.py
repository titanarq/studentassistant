"""`SessionService` per user: whose a session is, and what another user is told (#550, stage 1).

Two students share one vault and one backend, and the backend still has room for one active
capture session. What these tests walk is the boundary between them: that a session lives in the
folder of the student who started it, that the other student is refused without learning anything
about it, and that opening the vault finds an unended session of either of them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime, timedelta, tzinfo

import pytest

from studentassistant.protocol import PROTOCOL_VERSION, ErrorCode
from studentassistant.server.bus import SessionBus
from studentassistant.server.errors import ApiError
from studentassistant.server.session_routes import _http_errors
from studentassistant.server.sessions import (
    OTHER_USER_SESSION_OPEN_DETAIL,
    ActiveSessionExistsError,
    NoUserError,
    OtherUserSessionOpenError,
    SessionAlreadyEndedError,
    SessionService,
    UnknownSessionError,
    UnknownUserError,
)
from studentassistant.vault import (
    Vault,
    create_subject,
    create_topic,
    create_user,
    end_session,
    read_active_host,
    resume_session,
    start_session,
)
from studentassistant.vault import sessions as vault_sessions

pytestmark = pytest.mark.anyio

SUBJECT = "Física"
SUBJECT_ID = "fisica"
TOPIC = "Cinemática"
TOPIC_ID = "cinematica"

NEVER_WRITTEN = "20000101-000000"
"""A session id no topic of anybody's lists."""


class _Later(datetime):
    """`datetime` a minute ahead, so a second session gets an id of its own."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return datetime.now(tz) + timedelta(minutes=1)


@contextlib.contextmanager
def _a_minute_later() -> Iterator[None]:
    """Shift the vault's clock a minute for the block: a session id is only a second wide.

    Two students cannot really start a session in the same second on one backend -- the second
    start is refused while the first is unended -- but a test that ends one and starts the other's
    inside the same second would give both the same id, and the second session would then be
    indistinguishable from the first.
    """
    real = vault_sessions.datetime
    vault_sessions.datetime = _Later  # type: ignore[misc]
    try:
        yield
    finally:
        vault_sessions.datetime = real  # type: ignore[misc]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def service(tmp_vault: Vault) -> SessionService:
    return SessionService(SessionBus(), vault=tmp_vault, host="pc-test")


@pytest.fixture
def two_users(tmp_vault: Vault, student_user_id: str) -> tuple[str, str]:
    """The ids of the two students `tmp_vault` holds: the one it was born with and a new one."""
    return student_user_id, create_user(tmp_vault, "Lucía Martín").id


def _topic_in(vault: Vault) -> tuple[str, str]:
    """A subject with one topic written straight into `vault`, through the vault's own functions."""
    subject = create_subject(vault, SUBJECT)
    topic = create_topic(vault, subject.slug, TOPIC)
    return subject.slug, topic.slug


def _unended_in(vault: Vault, host: str = "other-pc", later: bool = False) -> str:
    """An unended session of `vault`'s user, as a backend that stopped would leave it.

    `later` shifts the clock a minute, because a session id is a second wide and two students
    capturing at the same instant would otherwise share one.
    """
    subject_id, topic_id = _topic_in(vault)
    with contextlib.ExitStack() as stack:
        if later:
            stack.enter_context(_a_minute_later())
        return start_session(vault, subject_id, topic_id, host, PROTOCOL_VERSION).id


async def _topic(service: SessionService, user_id: str) -> tuple[str, str]:
    """A subject with one topic, created through the service as `user_id`; their slugs."""
    subject = await service.create_subject(user_id, SUBJECT)
    topic = await service.create_topic(user_id, subject.subject_id, TOPIC)
    return subject.subject_id, topic.topic_id


async def _started(service: SessionService, user_id: str) -> str:
    subject_id, topic_id = await _topic(service, user_id)
    return (await service.start(user_id, subject_id, topic_id, client_time_ms=1)).session_id


# whose a session is


async def test_a_session_belongs_to_the_user_it_was_started_for(
    service: SessionService, two_users: tuple[str, str], tmp_vault: Vault
) -> None:
    mine, theirs = two_users
    session_id = await _started(service, mine)

    active = service.active
    assert active is not None
    assert (active.session_id, active.user_id) == (session_id, mine)
    assert await service.get_active(mine, session_id) == active
    assert await service.get_active(theirs, session_id) is None
    # The handle the consumers write through is the user's, so nothing they write can land in
    # another student's folder or at the repository root.
    handle = await service.require_active(mine, session_id)
    assert handle.vault.user_id == mine
    assert handle.vault.path == tmp_vault.root / "users" / mine
    assert handle.directory.relative_to(tmp_vault.root).parts[:2] == ("users", mine)
    # And the vault's own record of who is capturing names them.
    claim = read_active_host(tmp_vault)
    assert claim is not None
    assert (claim.user, claim.session_id) == (mine, session_id)


async def test_a_session_of_another_user_is_unknown_not_theirs(
    service: SessionService, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    session_id = await _started(service, mine)

    with pytest.raises(UnknownSessionError):
        await service.require_active(theirs, session_id)
    with pytest.raises(UnknownSessionError):
        await service.resume(theirs, session_id)
    assert await service.is_known(mine, session_id)
    assert not await service.is_known(theirs, session_id)
    # The other student is told exactly what they are told of an id nobody ever wrote, so the
    # answer cannot be used to probe for session ids that are somebody else's.
    with pytest.raises(UnknownSessionError) as theirs_refused:
        await service.require_active(theirs, session_id)
    with pytest.raises(UnknownSessionError) as nobody:
        await service.require_active(theirs, NEVER_WRITTEN)
    assert str(theirs_refused.value).replace(session_id, NEVER_WRITTEN) == str(nobody.value)


async def test_two_users_topics_with_the_same_slugs_are_two_topics(
    service: SessionService, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    subject_id, topic_id = await _topic(service, mine)
    their_subject, their_topic = await _topic(service, theirs)
    assert (subject_id, topic_id) == (their_subject, their_topic) == (SUBJECT_ID, TOPIC_ID)

    started = await service.start(mine, subject_id, topic_id, client_time_ms=1)

    # Each student lists their own topic, and only the one who owns the session sees it open.
    mine_listed = (await service.list_topics(mine, subject_id)).topics
    theirs_listed = (await service.list_topics(theirs, their_subject)).topics
    assert mine_listed[0].open_session_id == started.session_id
    assert theirs_listed[0].open_session_id is None
    assert await service.open_session_of(mine, subject_id, topic_id) == started.session_id
    assert await service.open_session_of(theirs, their_subject, their_topic) is None


# the one active session of the backend


async def test_a_start_by_another_user_is_refused_without_naming_the_session(
    service: SessionService, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    session_id = await _started(service, mine)
    their_subject, their_topic = await _topic(service, theirs)

    with pytest.raises(OtherUserSessionOpenError) as refused:
        await service.start(theirs, their_subject, their_topic, client_time_ms=2)

    assert str(refused.value) == OTHER_USER_SESSION_OPEN_DETAIL
    assert session_id not in str(refused.value)
    # Not an `ActiveSessionExistsError`, which is the one that carries the id a header would leak.
    assert not isinstance(refused.value, ActiveSessionExistsError)
    assert not hasattr(refused.value, "session_id")
    # The student the session belongs to still gets today's answer, naming it.
    own_other = await service.create_topic(mine, SUBJECT_ID, "Dinámica")
    with pytest.raises(ActiveSessionExistsError) as own_refused:
        await service.start(mine, SUBJECT_ID, own_other.topic_id, client_time_ms=3)
    assert own_refused.value.session_id == session_id


async def test_a_resume_by_another_user_is_refused_without_naming_the_session(
    service: SessionService, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    session_id = await _started(service, mine)
    their_subject, their_topic = await _topic(service, theirs)
    await service.end(session_id, client_time_ms=2, reason="button")
    with _a_minute_later():
        theirs_id = (
            await service.start(theirs, their_subject, their_topic, client_time_ms=3)
        ).session_id
    assert theirs_id != session_id

    with pytest.raises(OtherUserSessionOpenError) as refused:
        await service.resume(mine, session_id)

    assert str(refused.value) == OTHER_USER_SESSION_OPEN_DETAIL
    assert theirs_id not in str(refused.value)


async def test_ending_a_session_frees_the_backend_for_the_other_user(
    service: SessionService, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    session_id = await _started(service, mine)
    their_subject, their_topic = await _topic(service, theirs)

    await service.end(session_id, client_time_ms=2, reason="button")

    started = await service.start(theirs, their_subject, their_topic, client_time_ms=3)
    active = service.active
    assert active is not None
    assert (active.session_id, active.user_id) == (started.session_id, theirs)
    with pytest.raises(UnknownSessionError):
        await service.require_active(mine, started.session_id)


# opening the vault scans every user


async def test_opening_the_vault_finds_an_unended_session_of_another_user(
    tmp_vault: Vault, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    left = _unended_in(tmp_vault.for_user(mine))
    service = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")
    their_subject, their_topic = await _topic(service, theirs)

    # Found, and it is the student's own topic that lists it.
    assert await service.open_session_of(mine, SUBJECT_ID, TOPIC_ID) == left
    assert await service.open_session_of(theirs, their_subject, their_topic) is None
    assert await service.is_known(mine, left)
    assert not await service.is_known(theirs, left)
    # One active session per backend, so it blocks a start by the student who has none.
    with pytest.raises(OtherUserSessionOpenError) as refused:
        await service.start(theirs, their_subject, their_topic, client_time_ms=1)
    assert left not in str(refused.value)
    # Its owner is named it, and can end it, which frees the backend for the other.
    with pytest.raises(ActiveSessionExistsError) as own:
        await service.start(mine, SUBJECT_ID, TOPIC_ID, client_time_ms=1)
    assert own.value.session_id == left
    await service.end(left, client_time_ms=2, reason="button")
    started = await service.start(theirs, their_subject, their_topic, client_time_ms=3)
    assert service.active is not None and service.active.user_id == theirs
    assert started.session_id


async def test_a_scan_that_found_two_unended_sessions_names_each_user_their_own(
    tmp_vault: Vault, two_users: tuple[str, str]
) -> None:
    """The state two PCs that both stopped without ending leave behind.

    A backend has room for one active session, so both block a start; what each student is told is
    their own session, because it is the one they can still resume or end.
    """
    mine, theirs = two_users
    left = {
        mine: _unended_in(tmp_vault.for_user(mine)),
        theirs: _unended_in(tmp_vault.for_user(theirs), later=True),
    }
    assert left[mine] != left[theirs]
    service = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")

    for user_id in two_users:
        listed = (await service.list_topics(user_id, SUBJECT_ID)).topics
        assert listed[0].open_session_id == left[user_id]
        assert await service.is_known(user_id, left[user_id])
        other = theirs if user_id == mine else mine
        assert not await service.is_known(user_id, left[other])
        with pytest.raises(ActiveSessionExistsError) as refused:
            await service.start(user_id, SUBJECT_ID, TOPIC_ID, client_time_ms=1)
        assert refused.value.session_id == left[user_id]
    # Either one can be resumed by its owner, and only by them.
    with pytest.raises(UnknownSessionError):
        await service.resume(theirs, left[mine])
    resumed = await service.resume(mine, left[mine])
    assert resumed.session_id == left[mine]
    assert resume_session(tmp_vault.for_user(mine), SUBJECT_ID, TOPIC_ID).id == left[mine]


async def test_an_ended_session_of_another_user_is_still_not_theirs(
    tmp_vault: Vault, two_users: tuple[str, str]
) -> None:
    mine, theirs = two_users
    subject_id, topic_id = _topic_in(tmp_vault.for_user(mine))
    ended = start_session(tmp_vault.for_user(mine), subject_id, topic_id, "pc", PROTOCOL_VERSION)
    end_session(ended)
    service = SessionService(SessionBus(), vault=tmp_vault, host="pc-test")

    assert await service.is_known(mine, ended.id)
    assert not await service.is_known(theirs, ended.id)
    with pytest.raises(SessionAlreadyEndedError):
        await service.resume(mine, ended.id)
    with pytest.raises(UnknownSessionError):
        await service.resume(theirs, ended.id)


# naming the user


async def test_a_call_that_names_no_user_needs_the_vault_to_hold_exactly_one(
    tmp_vault: Vault, service: SessionService, student_user_id: str
) -> None:
    """Protocol 1.8's single-user fallback, which is what the routes still rely on."""
    await service.create_subject(None, SUBJECT)
    assert [s.subject_id for s in (await service.list_subjects(None)).subjects] == [SUBJECT_ID]

    second = create_user(tmp_vault, "Lucía Martín").id
    with pytest.raises(NoUserError):
        await service.list_subjects(None)
    # Naming one of them is enough, and each sees only their own.
    assert [s.subject_id for s in (await service.list_subjects(student_user_id)).subjects] == [
        SUBJECT_ID
    ]
    assert (await service.list_subjects(second)).subjects == []


async def test_a_user_the_vault_does_not_have_is_refused(service: SessionService) -> None:
    with pytest.raises(UnknownUserError) as refused:
        await service.list_subjects("nadie")
    assert refused.value.user_id == "nadie"


async def test_a_user_handle_is_widened_to_the_repository_it_belongs_to(
    user_vault: Vault, student_user_id: str
) -> None:
    """A service given one student's handle still works on the whole repository.

    `create_app` is handed whichever handle its caller has, and `for_user` needs a root one, so the
    service widens it back: git, the locks and `.sa/active.yaml` stay the repository's business.
    """
    create_subject(user_vault, SUBJECT)
    service = SessionService(SessionBus(), vault=user_vault, host="pc-test")

    root = await service.open_vault()
    assert (root.user_id, root.path) == (None, user_vault.root)
    assert [s.subject_id for s in (await service.list_subjects(student_user_id)).subjects] == [
        SUBJECT_ID
    ]
    topic = await service.create_topic(student_user_id, SUBJECT_ID, TOPIC)
    started = await service.start(student_user_id, SUBJECT_ID, topic.topic_id, client_time_ms=1)
    assert service.active is not None and service.active.user_id == student_user_id
    assert started.session_id


# the refusal as a route answers it


async def test_the_other_user_refusal_is_409_session_open_with_no_open_session_header() -> None:
    """What `session_routes` turns `OtherUserSessionOpenError` into (#550).

    The same status and the same code a start over one's own unended session gets, so a client
    handles both the way it already does; the difference is the Spanish `detail` and the missing
    `X-Open-Session-Id`, which is what keeps another user's session id off the wire.
    """
    with pytest.raises(ApiError) as raised:
        async with _http_errors():
            raise OtherUserSessionOpenError()
    error = raised.value
    assert error.status_code == 409
    assert error.code is ErrorCode.SESSION_OPEN
    assert error.detail == OTHER_USER_SESSION_OPEN_DETAIL
    assert "X-Open-Session-Id" not in (error.headers or {})

    with pytest.raises(ApiError) as own:
        async with _http_errors():
            raise ActiveSessionExistsError("still open", "20260101-000000")
    assert own.value.headers is not None
    assert own.value.headers["X-Open-Session-Id"] == "20260101-000000"
