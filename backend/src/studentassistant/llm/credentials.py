"""Where the `anthropic` SDK would find credentials on this machine, without reading any secret.

Besides `ANTHROPIC_API_KEY`, the SDK authenticates through an `ant auth` profile: the profile
named by `ANTHROPIC_PROFILE`, else by `<config_dir>/active_config`, else `default`, described by
`<config_dir>/configs/<profile>.json`; `<config_dir>` is `ANTHROPIC_CONFIG_DIR` or
`~/.config/anthropic`. `find_ant_profile` mirrors that lookup so `studentassistant doctor` can
say a profile is there. It only checks that the (non-secret) profile config exists; it never
opens `credentials/`, runs the `ant` CLI or calls the API -- `check_api_key()` with no key is
what proves the profile works.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PROFILE_ENV_VAR = "ANTHROPIC_PROFILE"
CONFIG_DIR_ENV_VAR = "ANTHROPIC_CONFIG_DIR"
DEFAULT_PROFILE = "default"


def ant_config_dir(environ: Mapping[str, str], home: Path) -> Path:
    """`ANTHROPIC_CONFIG_DIR`, else `<home>/.config/anthropic` (the SDK's Linux/macOS default)."""
    configured = environ.get(CONFIG_DIR_ENV_VAR)
    return Path(configured) if configured else home / ".config" / "anthropic"


def _valid_profile_name(name: str) -> bool:
    return (
        bool(name)
        and name == name.strip()
        and not name.startswith(".")
        and not any(separator in name for separator in ("/", "\\", os.sep, "\x00"))
    )


def find_ant_profile(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> str | None:
    """The name of the `ant auth` profile the SDK would use, or `None` when there is none.

    `environ` and `home` default to the process environment and the user's home (tests pass
    their own). A profile counts when its `configs/<profile>.json` is a file.
    """
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    config_dir = ant_config_dir(environ, home)
    name = environ.get(PROFILE_ENV_VAR) or None
    if name is None:
        try:
            name = (config_dir / "active_config").read_text(encoding="utf-8").strip() or None
        except OSError:
            name = None
    name = name or DEFAULT_PROFILE
    if not _valid_profile_name(name):
        return None
    try:
        found = (config_dir / "configs" / f"{name}.json").is_file()
    except OSError:
        return None
    return name if found else None
