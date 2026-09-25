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


def texts(service: SessionService, query: str) -> list[str]:
    assert service.index is not None
    return [hit.path for hit in service.index.search(query)]


async def test_the_index_opens_with_the_vault_off_the_loop_at_index_path(
    tmp_vault: Vault, index_path: Path
) -> None:
    create_subject(tmp_vault, "Física")
    service = make_service(tmp_vault, index_path)
    assert service.index is None

    await service.open_vault()

    assert service.index is not None and service.index.path == index_path
    assert index_path.is_file()
    assert [s.slug for s in service.index.subjects()] == ["fisica"]
    assert not service.index_running  # not serving: no background loop
    await service.shutdown()
    assert service.index is None


async def test_the_background_loop_keeps_the_index_current_and_stops_at_shutdown(
    tmp_vault: Vault, index_path: Path
) -> None:
    subject = create_subject(tmp_vault, "Física").slug
    topic = create_topic(tmp_vault, subject, "Cinemática").slug
    service = make_service(tmp_vault, index_path, interval=0.01)
    await service.startup()
    try:
        await service.open_vault()
        assert service.index_running
        assert texts(service, "caída libre") == []

        page = put_source(tmp_vault, subject, topic, "web", "Caída", "# Caída libre\n", {})

        relative = page.relative_to(tmp_vault.path).as_posix()
        await eventually(lambda: texts(service, "caída libre") == [relative])
    finally:
        await service.shutdown()
    assert not service.index_running and service.index is None


def clone(origin: Path, path: Path) -> Vault:
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(path)], check=True, capture_output=True
    )
    return Vault.open(path)


async def test_the_pull_at_session_start_is_followed_by_an_index_refresh(
    tmp_vault: Vault, git_origin: Path, tmp_path: Path, index_path: Path
) -> None:
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    GitSync(tmp_vault, SETTINGS, clock=ManualClock()).flush()
    pc_b = clone(git_origin, tmp_path / "pc-b")
    service = make_service(
        tmp_vault, index_path, sync=GitSync(tmp_vault, SETTINGS, clock=ManualClock())
    )
    await service.open_vault()  # indexed before PC B's session exists; no background loop
    on_b = start_session(pc_b, "fisica", "cinematica", "pc-b", "1.0")
    on_b.append_transcript(0, 2_000, "El tiro parabólico combina dos movimientos.")
    end_session(on_b)
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()
    assert texts(service, "parabólico") == []

    await service.start("fisica", "cinematica", client_time_ms=0)
    await asyncio.wait_for(service.wait_index_refreshed(), timeout=10)

    assert service.index is not None
    (hit,) = service.index.search("parabólico")
    assert hit.session == on_b.id
    await service.shutdown()


async def test_an_index_that_cannot_open_leaves_search_off_but_the_vault_usable(
    tmp_vault: Vault, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unusable = tmp_path / "a-directory"
    unusable.mkdir()
    service = make_service(tmp_vault, unusable)
    await service.startup()

    await service.create_subject("Física")

    assert service.index is None and not service.index_running
    assert "search index" in caplog.text
    await service.shutdown()
