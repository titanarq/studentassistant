"""The Anthropic API key on this PC: a `KEY=value` file readable by its owner only.

`studentassistant setup` stores the key here (never in the vault, never in `config.toml`), and
`studentassistant serve` exports it as `ANTHROPIC_API_KEY` when the environment does not already
carry one, which is where the `anthropic` SDK looks. The file is also valid as a systemd
`EnvironmentFile`. Nothing here ever prints the key.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
# The key file is readable and writable by its owner only.
API_KEY_FILE_MODE = 0o600


def read_env_file(path: Path) -> dict[str, str]:
    """The `KEY=value` pairs of `path` (blank lines and `#` comments skipped); empty if absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def read_api_key(path: Path) -> str | None:
    """The key stored in `path`, or `None` when there is none."""
    return read_env_file(path).get(API_KEY_ENV_VAR) or None


def store_api_key(path: Path, key: str) -> bool:
    """Write `key` into `path` (mode 600), keeping any other line; return whether it changed."""
    key = key.strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("an API key is one word with no spaces")
    lines: list[str] = []
    replaced = False
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            name = raw.partition("=")[0].strip()
            if name == API_KEY_ENV_VAR and "=" in raw and not raw.lstrip().startswith("#"):
                if not replaced:
                    lines.append(f"{API_KEY_ENV_VAR}={key}")
                    replaced = True
                continue
            lines.append(raw)
    if not replaced:
        lines.append(f"{API_KEY_ENV_VAR}={key}")
    updated = "\n".join(lines) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == updated:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, API_KEY_FILE_MODE)
    try:
        os.fchmod(descriptor, API_KEY_FILE_MODE)  # the umask never widens it
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


def export_api_key(path: Path, environ: MutableMapping[str, str] | None = None) -> bool:
    """Set `ANTHROPIC_API_KEY` from `path` unless the environment has one; return whether it did."""
    environ = os.environ if environ is None else environ
    if environ.get(API_KEY_ENV_VAR):
        return False
    key = read_api_key(path)
    if key is None:
        return False
    environ[API_KEY_ENV_VAR] = key
    return True


def file_is_private(path: Path) -> bool:
    """Whether nobody but the owner can read or write `path`."""
    return path.stat().st_mode & 0o077 == 0
