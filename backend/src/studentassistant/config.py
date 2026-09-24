"""Backend configuration: a TOML file with `SA_*` environment variables on top of it.

The TOML file is `~/.config/studentassistant/config.toml` unless `SA_CONFIG` points somewhere else.
Every field can also be set through an `SA_`-prefixed environment variable, nesting levels separated
by `__` (`SA_SERVER__PORT=9000`, `SA_LLM__ROLES__EDITOR__MODEL=claude-opus-5-5`); the environment
always wins over the file. Model ids, paths and defaults live here and nowhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_PATH = Path("~/.config/studentassistant/config.toml")
DEFAULT_VAULT_PATH = Path("~/StudentAssistant/vault")
# Paired capture clients: machine-local state, never inside the vault.
DEFAULT_DEVICES_PATH = Path("~/.local/share/studentassistant/devices.json")

# Claude roles: the observer reads the live session, the editor and the generators write (ADR-0004).
FAST_MODEL = "claude-sonnet-5"
CAPABLE_MODEL = "claude-opus-5-5"


class ServerSettings(BaseModel):
    """Where the FastAPI app listens."""

    # ADR-0001: the backend binds the LAN interfaces so the phone and the web page can reach it;
    # pairing and the bearer token, not the bind address, are what protect it.
    model_config = ConfigDict(validate_default=True)

    host: str = "0.0.0.0"
    port: int = 8765
    # A loopback client (the web UI on the PC itself) needs no bearer token while this is true.
    trust_localhost: bool = True
    # The paired devices and their salted token hashes (file mode 600).
    devices_path: Path = DEFAULT_DEVICES_PATH
    # The LAN base URL put in the pairing QR; unset, it is `http://<this PC's LAN address>:<port>`.
    public_url: str | None = None
    # Extra names a request's `Host` header may carry (e.g. `mypc.local`), beyond `localhost`,
    # loopback/private IP literals, `host` and `public_url`'s host: the DNS-rebinding allowlist.
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("devices_path")
    @classmethod
    def expand_user(cls, path: Path) -> Path:
        return path.expanduser()


class VaultSettings(BaseModel):
    """Where the private git repository holding every piece of content lives (ADR-0002)."""

    # The default itself carries a `~`, so the validator has to run on defaults too.
    model_config = ConfigDict(validate_default=True)

    path: Path = DEFAULT_VAULT_PATH

    @field_validator("path")
    @classmethod
    def expand_user(cls, path: Path) -> Path:
        return path.expanduser()


class LlmRoleSettings(BaseModel):
    """One Claude role: the model that answers it."""

    model: str


class LlmRolesSettings(BaseModel):
    """Which model each role uses."""

    observer: LlmRoleSettings = Field(default_factory=lambda: LlmRoleSettings(model=FAST_MODEL))
    transcriber: LlmRoleSettings = Field(default_factory=lambda: LlmRoleSettings(model=FAST_MODEL))
    editor: LlmRoleSettings = Field(default_factory=lambda: LlmRoleSettings(model=CAPABLE_MODEL))
    generator: LlmRoleSettings = Field(default_factory=lambda: LlmRoleSettings(model=CAPABLE_MODEL))


class LlmSettings(BaseModel):
    """Claude client configuration (ADR-0004)."""

    roles: LlmRolesSettings = Field(default_factory=LlmRolesSettings)


# Speech-to-text (ADR-0008): by default the capture client transcribes (Google) and sends segments.
DEFAULT_STT_MODE = "client"
DEFAULT_STT_PROVIDER = "web-speech"
DEFAULT_STT_LANGUAGE = "es"


class SttSettings(BaseModel):
    """Which speech-to-text path and provider a session uses (ADR-0008)."""

    # `client`: the capture client transcribes and sends segments; `server`: it streams audio and
    # a `SpeechToTextProvider` registered under `provider` transcribes it here.
    mode: Literal["client", "server"] = DEFAULT_STT_MODE
    # `web-speech`, `android-speech`, `faster-whisper`, a cloud id, `fake` in tests.
    provider: str = DEFAULT_STT_PROVIDER
    language: str = DEFAULT_STT_LANGUAGE
    # Free-form settings per provider, keyed by provider name: `[stt.options.faster-whisper]`.
    options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def provider_options(self, name: str | None = None) -> dict[str, Any]:
        """The options table of `name` (the configured provider by default); empty when absent."""
        return dict(self.options.get(name or self.provider, {}))


def config_toml_path() -> Path:
    """The TOML file to read: `SA_CONFIG` when it is set, the default location otherwise."""
    return Path(os.environ.get("SA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


class Settings(BaseSettings):
    """The whole backend configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SA_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    stt: SttSettings = Field(default_factory=SttSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_toml_path()),
        )
