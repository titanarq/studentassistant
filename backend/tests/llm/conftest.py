"""Fixtures of the llm tests: settings that never read the student's real configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.config import Settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Default settings: `SA_CONFIG` points at a file that does not exist."""
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    return Settings()
