"""Single active writer in the server: session start claims, end releases, the status route warns.

Two PCs share one local bare repository (`git_origin`): `tmp_vault` is PC A, served by the
service under test, and `pc_b` a clone of it. Clocks are manual; nothing touches the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from studentassistant.config import ServerSettings, VaultGitSettings
from studentassistant.server.app import create_app
from studentassistant.server.bus import SessionBus
from studentassistant.server.pairing import PairingCodes
from studentassistant.server.sessions import SessionService
from studentassistant.vault import (
    GitSync,
    Vault,
    claim_active_host,
    create_subject,
    create_topic,
    read_active_host,
)

pytestmark = pytest.mark.anyio

SETTINGS = VaultGitSettings(commit_quiet_seconds=5, push_debounce_seconds=30)
SUBJECT = "subjects/fisica/subject.yaml"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ManualClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def remote_file(origin: Path, path: str) -> str:
    return git(origin, "show", f"main:{path}")


@pytest.fixture
def sync(tmp_vault: Vault, git_origin: Path) -> GitSync:
    return GitSync(tmp_vault, SETTINGS, clock=ManualClock())


@pytest.fixture
def pc_b(tmp_vault: Vault, git_origin: Path, tmp_path: Path, sync: GitSync) -> Vault:
    create_subject(tmp_vault, "Física")
    create_topic(tmp_vault, "fisica", "Cinemática")
    sync.flush()
    subprocess.run(
        ["git", "clone", "--quiet", str(git_origin), str(tmp_path / "pc-b")],
        check=True,
        capture_output=True,
    )
    return Vault.open(tmp_path / "pc-b")


@pytest.fixture
def service(tmp_vault: Vault, sync: GitSync, pc_b: Vault) -> SessionService:
    return SessionService(SessionBus(), vault=tmp_vault, sync=sync, host="pc-a")


async def test_the_start_claims_commits_and_asks_for_a_push_and_the_end_releases(
    service: SessionService, sync: GitSync, tmp_vault: Vault, git_origin: Path
) -> None:
    session = await service.start("fisica", "cinematica", client_time_ms=0)

    record = read_active_host(tmp_vault)
    assert record is not None
    assert (record.host, record.session_id) == ("pc-a", session.session_id)
    assert (record.subject, record.topic) == ("fisica", "cinematica")
    assert git(tmp_vault.path, "status", "--porcelain") == ""
    assert "iniciada en pc-a" in git(tmp_vault.path, "log", "-1", "--format=%s")
    sync.run_due()  # what the background loop does next: the push is already due
    assert "host: pc-a" in remote_file(git_origin, ".sa/active.yaml")

    await service.end(session.session_id, client_time_ms=1, reason="button")

    released = read_active_host(tmp_vault)
    assert released is not None and released.released
    assert "released_at: null" not in remote_file(git_origin, ".sa/active.yaml")


async def test_another_pcs_open_claim_warns_but_never_blocks_the_start(
    service: SessionService, pc_b: Vault, tmp_vault: Vault
) -> None:
    claim_active_host(pc_b, "pc-b", "20260925-090000", "fisica", "cinematica")
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()

    await service.list_subjects()  # opening the vault pulls and checks
    warning = service.host_warning
    assert warning is not None and warning.record.host == "pc-b"

    session = await service.start("fisica", "cinematica", client_time_ms=0)

    assert session.status == "active"
    assert service.host_warning is not None  # still pc-b's claim, as pulled
    record = read_active_host(tmp_vault)
    assert record is not None and record.host == "pc-a"  # this PC is the writer now


async def test_no_warning_without_another_pcs_claim(service: SessionService) -> None:
    await service.start("fisica", "cinematica", client_time_ms=0)
    assert service.host_warning is None


# the status route


@pytest.fixture
def app(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    sync: GitSync,
    pc_b: Vault,
) -> FastAPI:
    return create_app(
        static_dir=tmp_path / "no-web-build", server=server, codes=codes, vault=tmp_vault, sync=sync
    )


def client_of(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 5000))


def test_the_status_route_shows_the_host_warning(app: FastAPI, pc_b: Vault) -> None:
    claim_active_host(pc_b, "pc-b", "20260925-090000", "fisica", "cinematica")
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()

    body = client_of(app).get("/api/vault/status").json()

    assert body["host"] == app.state.sessions.host
    assert body["last_sync"]["outcome"] == "ok"
    assert body["divergence"] is None
    warning = body["host_warning"]
    assert warning["host"] == "pc-b"
    assert warning["session_id"] == "20260925-090000"
    assert "«pc-b»" in warning["message"]


def test_the_status_route_without_warning_or_divergence(app: FastAPI) -> None:
    body = client_of(app).get("/api/vault/status").json()

    assert body["host_warning"] is None
    assert body["divergence"] is None
    assert body["pending_commits"] == 0
    assert body["last_push_failure"] is None


def test_a_divergence_is_shown_with_both_versions(
    app: FastAPI, tmp_vault: Vault, pc_b: Vault
) -> None:
    (pc_b.path / SUBJECT).write_text("name: Física B\nstyle_guide: null\n")
    GitSync(pc_b, SETTINGS, clock=ManualClock()).flush()
    (tmp_vault.path / SUBJECT).write_text("name: Física A\nstyle_guide: null\n")
    client = client_of(app)

    body = client.get("/api/vault/status").json()

    divergence = body["divergence"]
    assert body["last_sync"]["outcome"] == "conflict"
    assert divergence["paths"] == [SUBJECT]
    assert SUBJECT in divergence["message"]
    versions = client.get("/api/vault/divergence", params={"path": SUBJECT}).json()
    assert versions == {
        "path": SUBJECT,
        "local": "name: Física A\nstyle_guide: null\n",
        "remote": "name: Física B\nstyle_guide: null\n",
    }
    missing = client.get("/api/vault/divergence", params={"path": "vault.yaml"})
    assert missing.status_code == 404
