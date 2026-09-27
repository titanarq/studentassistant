"""`[llm.roles.<role>]`: model, effort and max_tokens per role, with per-role defaults."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from studentassistant.config import Settings


@pytest.fixture
def config_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    for name in [
        "SA_LLM__ROLES__EDITOR__MODEL",
        "SA_LLM__ROLES__EDITOR__EFFORT",
        "SA_LLM__ROLES__OBSERVER__MAX_TOKENS",
        "SA_LLM__MAX_ATTEMPTS",
        "SA_LLM__ROLES__OBSERVER__TURN_TIMEOUT_SECONDS",
        "SA_LLM__ROLES__OBSERVER__MAX_ATTEMPTS",
        "SA_LLM__ROLES__EDITOR__TURN_TIMEOUT_SECONDS",
        "SA_LLM__ROLES__EDITOR__MAX_ATTEMPTS",
    ]:
        monkeypatch.delenv(name, raising=False)
    return path


def test_role_defaults(config_toml: Path) -> None:
    roles = Settings().llm.roles

    assert (roles.observer.model, roles.observer.effort) == ("claude-sonnet-5", "medium")
    assert (roles.transcriber.model, roles.transcriber.effort) == ("claude-sonnet-5", "medium")
    assert (roles.editor.model, roles.editor.effort) == ("claude-opus-5-5", "high")
    assert (roles.generator.model, roles.generator.effort) == ("claude-opus-5-5", "high")
    assert roles.observer.max_tokens == 16_000
    assert roles.editor.max_tokens == 64_000
    assert Settings().llm.max_attempts == 4


def test_a_partial_role_table_keeps_that_roles_other_defaults(config_toml: Path) -> None:
    config_toml.write_text(
        textwrap.dedent(
            """
            [llm.roles.observer]
            effort = "low"
            """
        ),
        encoding="utf-8",
    )

    observer = Settings().llm.roles.observer

    assert observer.effort == "low"
    assert observer.model == "claude-sonnet-5"
    assert observer.max_tokens == 16_000


def test_env_vars_set_effort_and_max_tokens(
    config_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__ROLES__EDITOR__EFFORT", "xhigh")
    monkeypatch.setenv("SA_LLM__ROLES__OBSERVER__MAX_TOKENS", "4000")

    roles = Settings().llm.roles

    assert roles.editor.effort == "xhigh"
    assert roles.editor.model == "claude-opus-5-5"
    assert roles.observer.max_tokens == 4000


def test_an_unknown_effort_is_rejected(config_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_LLM__ROLES__EDITOR__EFFORT", "turbo")

    with pytest.raises(ValidationError):
        Settings()


def test_turn_timeout_and_attempts_defaults_per_role(config_toml: Path) -> None:
    roles = Settings().llm.roles

    assert (roles.observer.turn_timeout_seconds, roles.observer.max_attempts) == (90.0, 2)
    for role in (roles.transcriber, roles.editor, roles.generator):
        assert (role.turn_timeout_seconds, role.max_attempts) == (None, None)


def test_turn_timeout_and_attempts_from_toml(config_toml: Path) -> None:
    config_toml.write_text(
        textwrap.dedent(
            """
            [llm.roles.observer]
            turn_timeout_seconds = 45
            max_attempts = 3

            [llm.roles.editor]
            turn_timeout_seconds = 900
            """
        ),
        encoding="utf-8",
    )

    roles = Settings().llm.roles

    assert (roles.observer.turn_timeout_seconds, roles.observer.max_attempts) == (45.0, 3)
    assert roles.observer.model == "claude-sonnet-5"
    assert (roles.editor.turn_timeout_seconds, roles.editor.max_attempts) == (900.0, None)
    assert roles.generator.turn_timeout_seconds is None


def test_turn_timeout_and_attempts_from_env(
    config_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__ROLES__OBSERVER__TURN_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("SA_LLM__ROLES__EDITOR__MAX_ATTEMPTS", "1")

    roles = Settings().llm.roles

    assert roles.observer.turn_timeout_seconds == 30.0
    assert roles.observer.max_attempts == 2
    assert roles.editor.max_attempts == 1


@pytest.mark.parametrize(("key", "value"), [("TURN_TIMEOUT_SECONDS", "0"), ("MAX_ATTEMPTS", "0")])
def test_a_non_positive_timeout_or_attempts_is_rejected(
    config_toml: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(f"SA_LLM__ROLES__OBSERVER__{key}", value)

    with pytest.raises(ValidationError):
        Settings()
