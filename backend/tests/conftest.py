"""The fixtures any backend test can ask for, wherever in `tests/` it lives."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.vault import Vault

# The name a test vault records in its `vault.yaml`; tests that assert on it import it from here.
STUDENT = "Ana García"


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """An initialised vault under `tmp_path`, for any test that needs somewhere to write content.

    `HOME` is moved into `tmp_path` first, because creating a vault runs `git init` and git reads
    the global configuration of whoever runs it: a test must neither depend on the student's home
    directory nor write into it. `XDG_CONFIG_HOME` goes with it, since git looks there before it
    looks in `~/.gitconfig`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return Vault.init(tmp_path / "vault", student=STUDENT)
