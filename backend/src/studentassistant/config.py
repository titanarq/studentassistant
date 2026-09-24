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


# Vault git sync (ADR-0002): commits are batched after a quiet period, pushes are debounced.
DEFAULT_COMMIT_QUIET_SECONDS = 30.0
DEFAULT_COMMIT_MAX_DELAY_SECONDS = 300.0
DEFAULT_PUSH_DEBOUNCE_SECONDS = 120.0
DEFAULT_PUSH_BACKOFF_INITIAL_SECONDS = 15.0
DEFAULT_PUSH_BACKOFF_MAX_SECONDS = 900.0
DEFAULT_GIT_TIMEOUT_SECONDS = 120.0
DEFAULT_VAULT_REMOTE = "origin"
# The author email a vault commit carries when none is configured: a reserved `.invalid` domain,
# so no real mailbox is ever claimed on the student's behalf.
DEFAULT_VAULT_AUTHOR_EMAIL = "estudiante@studentassistant.invalid"


class VaultGitSettings(BaseModel):
    """How the vault is committed, pushed and pulled (ADR-0002)."""

    # The identity every vault commit and tag is authored as. Unset, the name is the student's
    # display name recorded in `vault.yaml`.
    author_name: str | None = None
    author_email: str = DEFAULT_VAULT_AUTHOR_EMAIL
    remote: str = DEFAULT_VAULT_REMOTE
    # Pending changes are committed once nothing changed for this long...
    commit_quiet_seconds: float = Field(default=DEFAULT_COMMIT_QUIET_SECONDS, ge=0)
    # ...or, under a steady stream of changes, this long after the first uncommitted one.
    commit_max_delay_seconds: float = Field(default=DEFAULT_COMMIT_MAX_DELAY_SECONDS, ge=0)
    # A new commit is pushed this long after the first unpushed one (not reset by later ones).
    push_debounce_seconds: float = Field(default=DEFAULT_PUSH_DEBOUNCE_SECONDS, ge=0)
    # A failed push is retried after `initial`, doubling per consecutive failure up to `max`.
    push_backoff_initial_seconds: float = Field(default=DEFAULT_PUSH_BACKOFF_INITIAL_SECONDS, gt=0)
    push_backoff_max_seconds: float = Field(default=DEFAULT_PUSH_BACKOFF_MAX_SECONDS, gt=0)
    # A network git command (push, pull, ls-remote) is killed after this long.
    timeout_seconds: float = Field(default=DEFAULT_GIT_TIMEOUT_SECONDS, gt=0)


class VaultSettings(BaseModel):
    """Where the private git repository holding every piece of content lives (ADR-0002)."""

    # The default itself carries a `~`, so the validator has to run on defaults too.
    model_config = ConfigDict(validate_default=True)

    path: Path = DEFAULT_VAULT_PATH
    git: VaultGitSettings = Field(default_factory=VaultGitSettings)

    @field_validator("path")
    @classmethod
    def expand_user(cls, path: Path) -> Path:
        return path.expanduser()


# Effort is always sent explicitly: Opus 5.5 would otherwise default to `medium` (ADR-0004).
Effort = Literal["low", "medium", "high", "xhigh", "max"]
FAST_EFFORT: Effort = "medium"
CAPABLE_EFFORT: Effort = "high"
FAST_MAX_TOKENS = 16_000
CAPABLE_MAX_TOKENS = 64_000
# Attempts per call (the first one included) before a 429/5xx/connection error surfaces.
DEFAULT_LLM_MAX_ATTEMPTS = 4


class LlmRoleSettings(BaseModel):
    """One Claude role: the model that answers it, its effort and its output ceiling."""

    model: str
    effort: Effort = CAPABLE_EFFORT
    max_tokens: int = Field(default=CAPABLE_MAX_TOKENS, gt=0)


# One subclass per role, so a partial `[llm.roles.<role>]` table (or `SA_LLM__ROLES__...` variable)
# keeps that role's defaults for the keys it does not set.
class ObserverRoleSettings(LlmRoleSettings):
    model: str = FAST_MODEL
    effort: Effort = FAST_EFFORT
    max_tokens: int = Field(default=FAST_MAX_TOKENS, gt=0)


class TranscriberRoleSettings(LlmRoleSettings):
    model: str = FAST_MODEL
    effort: Effort = FAST_EFFORT
    max_tokens: int = Field(default=FAST_MAX_TOKENS, gt=0)


class EditorRoleSettings(LlmRoleSettings):
    model: str = CAPABLE_MODEL


class GeneratorRoleSettings(LlmRoleSettings):
    model: str = CAPABLE_MODEL


class LlmRolesSettings(BaseModel):
    """Which model, effort and max_tokens each role uses."""

    observer: ObserverRoleSettings = Field(default_factory=ObserverRoleSettings)
    transcriber: TranscriberRoleSettings = Field(default_factory=TranscriberRoleSettings)
    editor: EditorRoleSettings = Field(default_factory=EditorRoleSettings)
    generator: GeneratorRoleSettings = Field(default_factory=GeneratorRoleSettings)


class LlmSettings(BaseModel):
    """Claude client configuration (ADR-0004)."""

    roles: LlmRolesSettings = Field(default_factory=LlmRolesSettings)
    max_attempts: int = Field(default=DEFAULT_LLM_MAX_ATTEMPTS, ge=1)


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
