"""The vault's search index wired into the session service: open, background loop, refresh, close.

The index lives under `tmp_path`, every remote is a local bare repository (`git_origin`) and every
wait is bounded, so nothing here touches `~/.cache`, the network, or hangs.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from studentassistant.config import VaultGitSettings, VaultSettings
from studentassistant.server.bus import SessionBus
from studentassistant.server.sessions import SessionService
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    start_session,
)
from studentassistant.vault.index import VaultIndex, user_index_path

pytestmark = pytest.mark.anyio

SETTINGS = VaultGitSettings(
    commit_quiet_seconds=5, commit_max_delay_seconds=60, push_debounce_seconds=30
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ManualClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


async def eventually(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "index.sqlite3"


def make_service(
    vault: Vault, index_path: Path, *, sync: GitSync | None = None, interval: float = 5.0
) -> SessionService:
    return SessionService(
        SessionBus(),
        vault=vault,
        sync=sync,
        vault_settings=VaultSettings(path=vault.path, index_path=index_path),
        host="pc-a",
        sync_interval=0.01,
        index_interval=interval,
    )


def only_index(service: SessionService) -> VaultIndex:
    """The one user's index the service has opened (one database per user, #566)."""
    (index,) = service.indexes.values()
    return index


def texts(service: SessionService, query: str) -> list[str]:
    return [hit.path for hit in only_index(service).search(query)]


async def test_a_users_index_opens_lazily_off_the_loop_at_their_own_path(
    tmp_vault: Vault, user_vault: Vault, index_path: Path
) -> None:
    create_subject(user_vault, "Física")
    service = make_service(tmp_vault, index_path)
    assert service.indexes == {}

    await service.open_vault()
    assert service.indexes == {}  # the vault opening opens no index: the first use of a user does

    index = await service.index_of(None)

    assert index is not None and service.indexes == {user_vault.user_id: index}
    assert index.path == user_index_path(index_path, str(user_vault.user_id))
    assert index.path.is_file() and not index_path.exists()
    assert [s.slug for s in index.subjects()] == ["fisica"]
    assert not service.index_running  # not serving: no background loop
    await service.shutdown()
    assert service.indexes == {}


async def test_the_background_loop_keeps_the_index_current_and_stops_at_shutdown(
    tmp_vault: Vault, user_vault: Vault, index_path: Path
) -> None:
    subject = create_subject(user_vault, "Física").slug
    topic = create_topic(user_vault, subject, "Cinemática").slug
    service = make_service(tmp_vault, index_path, interval=0.01)
    await service.startup()
    try:
        await service.open_vault()
        assert not service.index_running  # no index yet: nothing to keep current
        await service.index_of(None)
        assert service.index_running
        assert texts(service, "caída libre") == []

        page = put_source(user_vault, subject, topic, "web", "Caída", "# Caída libre\n", {})

        relative = page.relative_to(user_vault.path).as_posix()
        await eventually(lambda: texts(service, "caída libre") == [relative])
    finally:
        await service.shutdown()
    assert not service.index_running and service.indexes == {}


def clone(origin: Path, path: Path) -> Vault:
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(path)], check=True, capture_output=True
    )
    return Vault.open(path)


async def test_the_pull_at_session_start_is_followed_by_an_index_refresh(
    tmp_vault: Vault, user_vault: Vault, git_origin: Path, tmp_path: Path, index_path: Path
) -> None:
    # The service starts a session for the vault's one user (#550), and indexes that student's
    # folder (#566): the topic is in it, and so is PC B's session below.
    create_subject(user_vault, "Física")
    create_topic(user_vault, "fisica", "Cinemática")
    GitSync(tmp_vault, SETTINGS, clock=ManualClock()).flush()
    pc_b = clone(git_origin, tmp_path / "pc-b")
    service = make_service(
        tmp_vault, index_path, sync=GitSync(tmp_vault, SETTINGS, clock=ManualClock())
    )
    # Indexed before PC B's session exists; no background loop.
    assert await service.index_of(None) is not None
    on_b = start_session(
        pc_b.for_user(str(user_vault.user_id)), "fisica", "cinematica", "pc-b", "1.0"
    )
    on_b.append_transcript(0, 2_000, "El tiro parabólico combina dos movimientos.")
    end_session(on_b)
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()
    assert texts(service, "parabólico") == []

    await service.start(None, "fisica", "cinematica", client_time_ms=0)
    await asyncio.wait_for(service.wait_index_refreshed(), timeout=10)

    (hit,) = only_index(service).search("parabólico")
    assert hit.session == on_b.id
    await service.shutdown()


async def test_an_index_that_cannot_open_leaves_search_off_but_the_vault_usable(
    tmp_vault: Vault, user_vault: Vault, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unusable = tmp_path / "index.sqlite3"
    user_index_path(unusable, str(user_vault.user_id)).mkdir()  # a directory in the way
    service = make_service(tmp_vault, unusable)
    await service.startup()

    await service.create_subject(None, "Física")

    assert await service.index_of(None) is None and not service.index_running
    assert "search index" in caplog.text
    await service.shutdown()
