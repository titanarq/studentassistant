"""`[llm] backend`: `auto` picks the API when a key is on this PC and Claude Code otherwise;
`check_claude_code` asks `claude auth status` once, never a model."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fake_claude_cli import install_fake_claude

from studentassistant.config import ClaudeCodeSettings, Settings
from studentassistant.llm import (
    AnthropicTransport,
    ClaudeCodeTransport,
    LLMAPIError,
    LLMConnectionError,
    check_claude_code,
    default_transport,
    get_client,
    resolve_backend,
)

KEY = "sk-" + "ant-" + "api03-" + "z" * 40


def no_profile() -> str | None:
    return None


def test_auto_without_any_key_is_claude_code(settings: Settings) -> None:
    assert resolve_backend(settings, environ={}, ant_profile=no_profile) == "claude-code"


def test_auto_with_a_key_in_the_environment_is_the_api(settings: Settings) -> None:
    environ = {"ANTHROPIC_API_KEY": KEY}
    assert resolve_backend(settings, environ=environ, ant_profile=no_profile) == "api"


def test_auto_with_the_key_file_or_a_profile_is_the_api(settings: Settings, tmp_path: Path) -> None:
    key_file = tmp_path / "secrets.env"
    key_file.write_text(f"# comment\nANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")
    with_file = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"api_key_file": key_file})}
    )
    assert resolve_backend(with_file, environ={}, ant_profile=no_profile) == "api"

    key_file.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    assert resolve_backend(with_file, environ={}, ant_profile=no_profile) == "claude-code"

    assert resolve_backend(settings, environ={}, ant_profile=lambda: "default") == "api"


@pytest.mark.parametrize("backend", ["api", "claude-code"])
def test_an_explicit_backend_wins(settings: Settings, backend: str) -> None:
    settings.llm.backend = backend  # type: ignore[assignment]
    environ = {"ANTHROPIC_API_KEY": KEY} if backend == "claude-code" else {}
    assert resolve_backend(settings, environ=environ, ant_profile=no_profile) == backend


def test_the_backend_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_LLM__BACKEND", "claude-code")
    monkeypatch.setenv("SA_LLM__CLAUDE_CODE__IDLE_TIMEOUT_SECONDS", "30")

    settings = Settings()

    assert settings.llm.backend == "claude-code"
    assert settings.llm.claude_code.idle_timeout_seconds == 30.0


def test_default_transport_and_get_client_follow_the_backend(settings: Settings) -> None:
    settings.llm.backend = "claude-code"
    transport = default_transport(settings)
    assert isinstance(transport, ClaudeCodeTransport)
    assert transport.process_count == 0  # nothing starts before the first call
    assert isinstance(get_client("observer", settings=settings).transport, ClaudeCodeTransport)

    settings.llm.backend = "api"
    assert isinstance(default_transport(settings), AnthropicTransport)


def test_check_claude_code_reads_auth_status(tmp_path: Path) -> None:
    fake = install_fake_claude(tmp_path / "fake").signed_in(subscriptionType="max")

    status = check_claude_code(ClaudeCodeSettings(executable=str(fake.executable)))

    assert status.logged_in and status.subscription_type == "max"
    assert status.auth_method == "claude.ai"
    assert fake.runs == []  # `auth status` only: no conversation, no model call


def test_check_claude_code_signed_out_missing_and_stuck(tmp_path: Path) -> None:
    fake = install_fake_claude(tmp_path / "fake")
    settings = ClaudeCodeSettings(executable=str(fake.executable))
    assert check_claude_code(settings).logged_in is False

    with pytest.raises(LLMAPIError, match="not on PATH"):
        check_claude_code(ClaudeCodeSettings(executable="no-such-claude-here"))

    def stuck(command: list[str], timeout: float) -> None:
        raise subprocess.TimeoutExpired(command, timeout)

    with pytest.raises(LLMConnectionError):
        check_claude_code(settings, which=lambda name: name, run=stuck)
