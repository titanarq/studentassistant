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
