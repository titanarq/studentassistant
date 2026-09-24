"""The git runner: author identity, pushing to a bare origin, redaction of credentials."""

from __future__ import annotations

from pathlib import Path

from git_helpers import ManualClock, git

from studentassistant.config import VaultGitSettings
from studentassistant.vault import Vault
from studentassistant.vault.git import GitIdentity, GitRunner, redact
from studentassistant.vault.sync import GitSync

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_commits_carry_the_configured_identity(tmp_vault: Vault) -> None:
    settings = VaultGitSettings(author_name="Ana G.", author_email="ana@example.invalid")
    sync = GitSync(tmp_vault, settings, clock=ManualClock())

    assert sync.checkpoint("primer commit") is not None

    author = git(tmp_vault.path, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip()
    assert author == "Ana G. <ana@example.invalid>|Ana G. <ana@example.invalid>"


def test_the_author_name_defaults_to_the_student_of_the_vault(tmp_vault: Vault) -> None:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    sync.checkpoint("primer commit")

    assert git(tmp_vault.path, "log", "-1", "--format=%an").strip() == tmp_vault.meta.student
    assert sync.identity.email == VaultGitSettings().author_email


def test_a_push_reaches_the_bare_origin(tmp_vault: Vault, git_origin: Path) -> None:
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    head = sync.checkpoint("primer commit")

    assert sync.push_now() is True
    assert git(git_origin, "rev-parse", "main").strip() == head
    assert sync.status().pending_commits == 0


def test_a_failing_command_is_a_result_not_an_exception(tmp_path: Path) -> None:
    runner = GitRunner(tmp_path, GitIdentity("Ana", "ana@example.invalid"))

    result = runner.run("rev-parse", "HEAD")

    assert not result.ok
    assert result.describe().startswith("git rev-parse exited")


def test_redact_removes_url_credentials_and_tokens() -> None:
    text = f"fatal: unable to access 'https://ana:{TOKEN}@github.com/ana/vault.git/': 403 {TOKEN}"

    redacted = redact(text)

    assert TOKEN not in redacted
    assert "ana:" not in redacted
    assert "https://***@github.com/ana/vault.git/" in redacted


def test_a_failed_push_never_shows_the_credentials_of_the_remote(tmp_vault: Vault) -> None:
    # Port 1 on loopback refuses at once: no network is involved, and git echoes the URL.
    git(tmp_vault.path, "remote", "add", "origin", f"https://ana:{TOKEN}@127.0.0.1:1/vault.git")
    sync = GitSync(tmp_vault, VaultGitSettings(timeout_seconds=20), clock=ManualClock())
    sync.checkpoint("primer commit")

    assert sync.push_now() is False

    failure = sync.status().last_push_failure
    assert failure is not None
    assert failure.kind == "offline"
    assert TOKEN not in failure.message
