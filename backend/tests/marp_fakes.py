"""A fake `marp` on a temporary `PATH`, so `doctor` finds Marp CLI without Node or a browser."""

from __future__ import annotations

import stat
from pathlib import Path

MARP_VERSION = "@marp-team/marp-cli v4.1.2 (w/ @marp-team/marp-core v4.0.1)"


def fake_marp_path(directory: Path, *, version: str = MARP_VERSION, exit_code: int = 0) -> str:
    """A `PATH` holding only a `marp` script that prints `version` and exits with `exit_code`."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "marp"
    script.write_text(f"#!/bin/sh\necho '{version}'\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(directory)
