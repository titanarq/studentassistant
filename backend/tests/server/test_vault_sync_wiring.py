"""The vault's `GitSync` wired into the server: background loop, shutdown flush, pulls.

Every remote is a local bare repository under `tmp_path` (`git_origin`) and every clock a manual
one, so nothing here touches the network or waits for a real quiet period.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import ServerSettings, VaultGitSettings
from studentassistant.server.app import create_app
from studentassistant.server.bus import SessionBus
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import SessionService, VaultSyncConflictError
from studentassistant.vault import GitSync, Vault, create_subject, create_topic, start_session

pytestmark = pytest.mark.anyio

SETTINGS = VaultGitSettings(
    commit_quiet_seconds=5, commit_max_delay_seconds=60, push_debounce_seconds=30
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ManualClock:
    """Seconds that only move when a test says so."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout


def commits(cwd: Path, ref: str = "HEAD") -> int:
    return int(git(cwd, "rev-list", "--count", ref).strip())


def remote_head(origin: Path) -> str | None:
    heads = git(origin, "for-each-ref", "--format=%(objectname)", "refs/heads/main").strip()
    return heads or None


def clone(origin: Path, path: Path) -> Vault:
    """A second PC: the vault cloned from `origin` into `path` and opened."""
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(path)], check=True, capture_output=True
    )
    return Vault.open(path)


async def eventually(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Wait (on the event loop, never blocking it) until the background loop made it true."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def sync(tmp_vault: Vault, git_origin: Path, clock: ManualClock) -> GitSync:
    return GitSync(tmp_vault, SETTINGS, clock=clock)


@pytest.fixture
def service(tmp_vault: Vault, sync: GitSync) -> SessionService:
    return SessionService(SessionBus(), vault=tmp_vault, sync=sync, host="pc-a", sync_interval=0.01)


# the background loop


async def test_sync_loop_commits_and_pushes_without_any_request(
    service: SessionService, tmp_vault: Vault, git_origin: Path, clock: ManualClock
) -> None:
    await service.startup()
    try:
        await service.create_subject("Física")
        assert service.sync_running
        before = commits(tmp_vault.path)

        clock.advance(SETTINGS.commit_quiet_seconds)
        await eventually(lambda: commits(tmp_vault.path) == before + 1)
        assert remote_head(git_origin) != git(tmp_vault.path, "rev-parse", "HEAD").strip()

        clock.advance(SETTINGS.push_debounce_seconds)
        head = git(tmp_vault.path, "rev-parse", "HEAD").strip()
        await eventually(lambda: remote_head(git_origin) == head)
    finally:
        await service.shutdown()
    assert not service.sync_running


async def test_sync_loop_waits_for_the_vault_to_open_and_for_the_app_to_serve(
    service: SessionService,
) -> None:
    await service.list_subjects()  # opened before serving: no loop yet
    assert not service.sync_running
    await service.startup()
    assert service.sync_running
    await service.shutdown()
    assert not service.sync_running


async def test_shutdown_flushes_pending_changes_in_a_worker_thread(
    service: SessionService, tmp_vault: Vault, git_origin: Path
) -> None:
    await service.startup()
    await service.create_subject("Física")

    await service.shutdown()

    assert git(tmp_vault.path, "status", "--porcelain") == ""
    assert remote_head(git_origin) == git(tmp_vault.path, "rev-parse", "HEAD").strip()
    assert not service.sync_running


# pulls at vault open and session start


@pytest.fixture
def pc_b(tmp_vault: Vault, git_origin: Path, tmp_path: Path) -> Vault:
    """A second PC sharing `git_origin`, cloned once the first one has pushed a topic."""
    bootstrap = GitSync(tmp_vault, SETTINGS, clock=ManualClock())
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    bootstrap.flush()
    return clone(git_origin, tmp_path / "pc-b")


async def test_the_vault_is_synced_before_the_scan_for_unended_sessions(
    service: SessionService, pc_b: Vault
) -> None:
    on_b = start_session(pc_b, "fisica", "cinematica", "pc-b", "1")
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()

    topics = await service.list_topics("fisica")

    assert topics.topics[0].open_session_id == on_b.id


async def test_a_sync_conflict_refuses_the_sessions_start_naming_the_paths(
    service: SessionService, tmp_vault: Vault, pc_b: Vault
) -> None:
    await service.list_subjects()
    (pc_b.path / "subjects" / "fisica" / "subject.yaml").write_text(
        "name: Física B\nstyle_guide: null\n"
    )
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()
    mine = "name: Física A\nstyle_guide: null\n"
    (tmp_vault.path / "subjects" / "fisica" / "subject.yaml").write_text(mine)

    with pytest.raises(VaultSyncConflictError) as refused:
        await service.start("fisica", "cinematica", client_time_ms=0)

    assert refused.value.conflicts == ("subjects/fisica/subject.yaml",)
    assert "subjects/fisica/subject.yaml" in str(refused.value)
    assert service.active is None
    assert (await service.list_topics("fisica")).topics[0].open_session_id is None
    # Nothing was auto-resolved: the local change is still there, committed, no rebase left.
    assert (tmp_vault.path / "subjects" / "fisica" / "subject.yaml").read_text() == mine
    assert git(tmp_vault.path, "status", "--porcelain") == ""


async def test_an_offline_remote_is_logged_and_the_sessions_start_proceeds(
    tmp_vault: Vault, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    git(tmp_vault.path, "remote", "add", "origin", str(tmp_path / "gone.git"))
    service = SessionService(
        SessionBus(), vault=tmp_vault, sync=GitSync(tmp_vault, SETTINGS, clock=ManualClock())
    )
    await service.create_subject("Física")
    await service.create_topic("fisica", "Cinemática")

    with caplog.at_level(logging.WARNING, logger="studentassistant.server.sessions"):
        session = await service.start("fisica", "cinematica", client_time_ms=0)

    assert session.status == "active"
    assert "session start: offline" in caplog.text


# the app's lifespan and REST


@pytest.fixture
def app(
    server: ServerSettings, codes: PairingCodes, tmp_path: Path, tmp_vault: Vault, sync: GitSync
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=tmp_vault, sync=sync
    )


def test_the_app_lifespan_runs_the_sync_loop_and_flushes_on_shutdown(
    app: FastAPI, tmp_vault: Vault, git_origin: Path
) -> None:
    service: SessionService = app.state.sessions
    with TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 5000)) as client:
        assert not service.sync_running  # creating/starting the app opens no vault
        assert client.post("/api/subjects", json={"name": "Física"}).status_code == 201
        assert service.sync_running
    assert not service.sync_running
    assert git(tmp_vault.path, "status", "--porcelain") == ""
    assert remote_head(git_origin) == git(tmp_vault.path, "rev-parse", "HEAD").strip()


def test_a_sync_conflict_answers_409_on_post_sessions(
    app: FastAPI, tmp_vault: Vault, pc_b: Vault
) -> None:
    (pc_b.path / "subjects" / "fisica" / "subject.yaml").write_text(
        "name: Física B\nstyle_guide: null\n"
    )
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()
    (tmp_vault.path / "subjects" / "fisica" / "subject.yaml").write_text(
        "name: Física A\nstyle_guide: null\n"
    )
    client = TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 5000))

    response = client.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 0},
    )

    assert response.status_code == 409
    assert "subjects/fisica/subject.yaml" in response.json()["detail"]
