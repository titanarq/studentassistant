"""Backend configuration: a TOML file with `SA_*` environment variables on top of it.

The TOML file is `~/.config/studentassistant/config.toml` unless `SA_CONFIG` points somewhere else.
Every field can also be set through an `SA_`-prefixed environment variable, nesting levels separated
by `__` (`SA_SERVER__PORT=9000`, `SA_LLM__ROLES__EDITOR__MODEL=claude-opus-5-5`); the environment
always wins over the file. Model ids, paths and defaults live here and nowhere else.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import UTC, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomlkit
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
# `serve --record`: one directory of raw client inputs per session, never inside the vault.
DEFAULT_RECORDINGS_DIR = Path("~/.cache/studentassistant/recordings")
# The derived SQLite index of the vault (ADR-0002): a cache, rebuildable from the vault at any time.
DEFAULT_INDEX_PATH = Path("~/.cache/studentassistant/index.sqlite3")
# `eval run`: the student's recorded sessions with reference notes; never in the code repo.
DEFAULT_EVAL_PATH = Path("~/StudentAssistant/evals")

# The API key file's name when `llm.api_key_file` is unset: next to the configuration file.
DEFAULT_API_KEY_FILE_NAME = "secrets.env"

# Claude roles: the observer reads the live session, the editor and the generators write (ADR-0004).
FAST_MODEL = "claude-sonnet-5"
CAPABLE_MODEL = "claude-opus-5-5"


DEFAULT_MAX_CAPTURE_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_CAPTURE_IMAGES = 5


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
    # `POST /api/sessions/{id}/captures`: the largest single image part a burst may carry, and
    # the most images one burst may hold; beyond either the upload is refused with 413.
    max_capture_image_bytes: int = Field(default=DEFAULT_MAX_CAPTURE_IMAGE_BYTES, ge=1)
    max_capture_images: int = Field(default=DEFAULT_MAX_CAPTURE_IMAGES, ge=1)
    # Where `studentassistant serve --record` writes each session's recording (what `replay`
    # reads back), one directory per session id; it must not be inside the vault.
    recordings_dir: Path = DEFAULT_RECORDINGS_DIR

    @field_validator("devices_path", "recordings_dir")
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
DEFAULT_ACTIVE_HOST_STALE_SECONDS = 6 * 3600.0
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
    # Another PC's active-host record (`.sa/active.yaml`) older than this is ignored as stale.
    active_host_stale_seconds: float = Field(default=DEFAULT_ACTIVE_HOST_STALE_SECONDS, gt=0)


class VaultPurgeSettings(BaseModel):
    """What `studentassistant purge` may drop from a topic whose notes were accepted (ADR-0003).

    Every kind can be switched off; nothing the current notes cite, no transcript and no notes
    history is ever a candidate, whatever is set here.
    """

    # Only purge a topic once its notes carry a version tag
    # `<subject-slug>/<topic-slug>/apuntes-vN`.
    require_notes_tag: bool = True
    # The stills of a burst other than the one kept as the page
    # (`sources/<kind>/page-NNN.burstK.*`, any source kind).
    burst_originals: bool = True
    # `conversations/observer-<session-id>.jsonl` of ended sessions (already rolled over).
    observer_conversations: bool = True
    # Session events already folded into the observer snapshot, replaced by that snapshot.
    folded_events: bool = True
    # Files under `generated/` last committed more than this many days ago; unset keeps them all.
    generated_max_age_days: int | None = Field(default=None, ge=1)


class VaultSettings(BaseModel):
    """Where the private git repository holding every piece of content lives (ADR-0002)."""

    # The default itself carries a `~`, so the validator has to run on defaults too.
    model_config = ConfigDict(validate_default=True)

    path: Path = DEFAULT_VAULT_PATH
    # The GitHub repository the vault is pushed to, `owner/name`; written by `studentassistant
    # setup`, unset until then.
    repo: str | None = None
    git: VaultGitSettings = Field(default_factory=VaultGitSettings)
    purge: VaultPurgeSettings = Field(default_factory=VaultPurgeSettings)
    # Where the derived search/listing index lives; never inside the vault.
    index_path: Path = DEFAULT_INDEX_PATH

    @field_validator("path", "index_path")
    @classmethod
    def expand_user(cls, path: Path) -> Path:
        return path.expanduser()

    @field_validator("repo")
    @classmethod
    def check_repo(cls, repo: str | None) -> str | None:
        if repo is not None:
            check_repo_name(repo)
        return repo


# A GitHub repository as `owner/name`: GitHub's own character sets for both halves.
REPO_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


def check_repo_name(repo: str) -> str:
    """Return `repo` when it is `owner/name`; raise `ValueError` otherwise."""
    if not REPO_NAME_PATTERN.fullmatch(repo) or repo.split("/")[1] in (".", ".."):
        raise ValueError(f"{repo!r} is not a GitHub repository written as owner/name")
    return repo


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


class LlmPrice(BaseModel):
    """What one model costs, in USD per million tokens of each kind the API reports."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    # 5-minute cache writes (`cache_control: ephemeral`), the only TTL this backend sets.
    cache_write_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float = Field(ge=0)


# Anthropic list prices of the two default models (USD per MTok): cache writes are 1.25x input
# (5-minute TTL), cache reads as published. A model missing here is recorded with no price.
DEFAULT_LLM_PRICES: dict[str, dict[str, float]] = {
    FAST_MODEL: {
        "input_per_mtok": 2.0,
        "output_per_mtok": 10.0,
        "cache_write_per_mtok": 2.5,
        "cache_read_per_mtok": 0.2,
    },
    CAPABLE_MODEL: {
        "input_per_mtok": 4.0,
        "output_per_mtok": 20.0,
        "cache_write_per_mtok": 5.0,
        "cache_read_per_mtok": 0.2,
    },
}


def _default_prices() -> dict[str, LlmPrice]:
    return {model: LlmPrice(**price) for model, price in DEFAULT_LLM_PRICES.items()}


# Claude's server-side web search and web fetch tools: the dynamic-filtering versions (Opus 4.6+,
# Sonnet 4.6+), and the web search price (USD per 1,000 searches).
DEFAULT_WEB_SEARCH_TOOL = "web_search_20260209"
DEFAULT_WEB_FETCH_TOOL = "web_fetch_20260209"
DEFAULT_WEB_SEARCH_USD_PER_THOUSAND = 10.0


class LlmSettings(BaseModel):
    """Claude client configuration (ADR-0004)."""

    roles: LlmRolesSettings = Field(default_factory=LlmRolesSettings)
    # The machine-local file (`KEY=value` lines, mode 600) `setup` stores the Anthropic API key
    # in and `serve` exports from; unset, `secrets.env` next to the configuration file. Never in
    # the vault, and never the key itself in `config.toml`.
    api_key_file: Path | None = None
    max_attempts: int = Field(default=DEFAULT_LLM_MAX_ATTEMPTS, ge=1)
    # Cost caps in USD (no cap when unset): the calls of one session, and every call of the
    # current UTC day across the whole vault. Only calls bound to a ledger count and are capped.
    max_usd_per_session: float | None = Field(default=None, ge=0)
    max_usd_per_day: float | None = Field(default=None, ge=0)
    # `[llm.prices."<model id>"]`: a configured table is merged over the defaults, key by key, so
    # adding a model or changing one price keeps the rest.
    prices: dict[str, LlmPrice] = Field(default_factory=_default_prices)
    # Claude's server-side web tools (`studentassistant.llm.web`): the tool versions sent, and the
    # price of each web search (USD per thousand searches; a web fetch costs only its tokens).
    web_search_tool: str = DEFAULT_WEB_SEARCH_TOOL
    web_fetch_tool: str = DEFAULT_WEB_FETCH_TOOL
    web_search_usd_per_thousand: float = Field(default=DEFAULT_WEB_SEARCH_USD_PER_THOUSAND, ge=0)

    @field_validator("api_key_file")
    @classmethod
    def expand_user(cls, path: Path | None) -> Path | None:
        return path.expanduser() if path is not None else None

    def api_key_path(self) -> Path:
        """Where the API key file is: `api_key_file`, or `secrets.env` beside the config file."""
        return self.api_key_file or config_toml_path().parent / DEFAULT_API_KEY_FILE_NAME

    @field_validator("prices", mode="before")
    @classmethod
    def merge_default_prices(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        merged: dict[str, Any] = {model: dict(price) for model, price in DEFAULT_LLM_PRICES.items()}
        for model, price in value.items():
            if isinstance(price, dict) and model in merged:
                merged[model] = {**merged[model], **price}
            else:
                merged[model] = price
        return merged


# Speech-to-text (ADR-0008): by default the capture client transcribes (Google) and sends segments.
DEFAULT_STT_MODE = "client"
DEFAULT_STT_PROVIDER = "web-speech"
DEFAULT_STT_LANGUAGE = "es"
# `[stt.options.faster-whisper]` keys `setup` and `doctor` read (ADR-0007): the model to
# download, the device (`auto` = CUDA when present, CPU otherwise; `cuda`; `cpu`) and where the
# model is cached (unset: the Hugging Face cache).
DEFAULT_WHISPER_MODEL = "large-v3-turbo"
DEFAULT_WHISPER_DEVICE = "auto"
# `[stt.options.google-cloud]` (Google Cloud Speech-to-Text v1 streaming): the recognition model.
DEFAULT_GOOGLE_SPEECH_MODEL = "latest_long"
# Server mode: seconds of audio queued for the provider past which superseded partials are dropped.
DEFAULT_STT_MAX_BACKLOG_SECONDS = 10.0
# Vocabulary hints (#54): at most this many terms (subject, topic, observer concepts), and this many
# characters joined with ", ". `max_terms = 0` turns them off. The protocol caps terms at 50.
DEFAULT_STT_VOCABULARY_MAX_TERMS = 30
DEFAULT_STT_VOCABULARY_MAX_CHARS = 500


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
    # Server mode: when more than this many seconds of audio wait for the provider, partials that a
    # newer segment supersedes are dropped (finals never are). `SA_STT__MAX_BACKLOG_SECONDS`.
    max_backlog_seconds: float = Field(default=DEFAULT_STT_MAX_BACKLOG_SECONDS, gt=0)
    # Vocabulary hints sent to the provider and the capture client: the most terms (0 = off, at
    # most 50) and characters. `SA_STT__VOCABULARY_MAX_TERMS`, `SA_STT__VOCABULARY_MAX_CHARS`.
    vocabulary_max_terms: int = Field(default=DEFAULT_STT_VOCABULARY_MAX_TERMS, ge=0, le=50)
    vocabulary_max_chars: int = Field(default=DEFAULT_STT_VOCABULARY_MAX_CHARS, ge=1)

    def provider_options(self, name: str | None = None) -> dict[str, Any]:
        """The options table of `name` (the configured provider by default); empty when absent."""
        return dict(self.options.get(name or self.provider, {}))


# PDF import (`studentassistant.sources.pdf`). The stored PDF goes to Claude base64-encoded (4/3 of
# its size) inside a request capped at 32 MB, and into plain git (no LFS, ADR-0002), hence 20 MB.
DEFAULT_MAX_PDF_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 100
DEFAULT_MAX_STORED_PDF_BYTES = 20 * 1024 * 1024
DEFAULT_PDF_THUMBNAIL_LONG_EDGE = 1200
DEFAULT_PDF_THUMBNAIL_QUALITY = 85
# Claude vision downscales an image whose long edge exceeds ~1568 px, so a bigger render only costs.
DEFAULT_PDF_TRANSCRIPTION_LONG_EDGE = 1568
DEFAULT_PDF_TRANSCRIPTION_QUALITY = 90
DEFAULT_CAPTURE_LONG_EDGE = 2400
DEFAULT_CAPTURE_JPEG_QUALITY = 85
DEFAULT_CAPTURE_WINDOW_BEFORE_SECONDS = 20.0
DEFAULT_CAPTURE_WINDOW_AFTER_SECONDS = 10.0
# Page transcription (`studentassistant.sources.transcriber`, role `transcriber`).
DEFAULT_TRANSCRIPTION_CONCURRENCY = 2
DEFAULT_TRANSCRIPTION_ATTEMPTS = 3
DEFAULT_TRANSCRIPTION_RETRY_SECONDS = 5.0
DEFAULT_TRANSCRIPTION_GRACE_SECONDS = 2.0
DEFAULT_TRANSCRIPTION_MIN_CROP_SHARE = 0.3
# Web search (`studentassistant.sources.web`, "busca esto en Internet").
DEFAULT_WEB_SEARCH_ROLE = "observer"
DEFAULT_WEB_SEARCH_MAX_USES = 3
DEFAULT_WEB_SEARCH_MAX_RESULTS = 5
DEFAULT_WEB_FETCH_MAX_CONTENT_TOKENS = 30_000
DEFAULT_WEB_SEARCH_CONCURRENCY = 1


class SourcesSettings(BaseModel):
    """Limits and rendering of imported sources (`[sources]`, `SA_SOURCES__*`)."""

    # The largest PDF accepted for import at all, before any page range is cut out of it.
    max_pdf_bytes: int = Field(default=DEFAULT_MAX_PDF_BYTES, ge=1)
    # The most pages one import may keep (Claude reads up to 600 pages per request, but every page
    # costs tokens on each editor call); a longer range is refused, never truncated.
    max_pdf_pages: int = Field(default=DEFAULT_MAX_PDF_PAGES, ge=1)
    # The largest PDF (the kept pages only) stored in the vault; beyond it the import is refused.
    max_stored_pdf_bytes: int = Field(default=DEFAULT_MAX_STORED_PDF_BYTES, ge=1)
    # Page thumbnails: long edge in pixels and JPEG quality.
    pdf_thumbnail_long_edge: int = Field(default=DEFAULT_PDF_THUMBNAIL_LONG_EDGE, ge=16)
    pdf_thumbnail_quality: int = Field(default=DEFAULT_PDF_THUMBNAIL_QUALITY, ge=1, le=100)
    # A scanned PDF page (no text layer) is rendered at this long edge (px) and JPEG quality for
    # its Claude vision transcription (`page-NNN.pKKK.md`, `sources/pdf_transcription.py`).
    pdf_transcription_long_edge: int = Field(default=DEFAULT_PDF_TRANSCRIPTION_LONG_EDGE, ge=16)
    pdf_transcription_quality: int = Field(default=DEFAULT_PDF_TRANSCRIPTION_QUALITY, ge=1, le=100)
    # Captured pages: the kept still and its page image are downscaled to this long edge (px)
    # and stored as JPEG at this quality.
    capture_long_edge: int = Field(default=DEFAULT_CAPTURE_LONG_EDGE, ge=16)
    capture_jpeg_quality: int = Field(default=DEFAULT_CAPTURE_JPEG_QUALITY, ge=1, le=100)
    # The transcript window a capture is linked to: this long before it to this long after it.
    capture_window_before_seconds: float = Field(
        default=DEFAULT_CAPTURE_WINDOW_BEFORE_SECONDS, ge=0
    )
    capture_window_after_seconds: float = Field(default=DEFAULT_CAPTURE_WINDOW_AFTER_SECONDS, ge=0)
    # Page transcription: off, `serve` never transcribes a page. A page waits until its transcript
    # window has passed (plus `transcription_grace_seconds`, for the last final to arrive); at
    # most `transcription_concurrency` pages go to Claude at once; a failed one is tried up to
    # `transcription_attempts` times, `transcription_retry_seconds` (doubling) apart.
    transcription_enabled: bool = True
    transcription_concurrency: int = Field(default=DEFAULT_TRANSCRIPTION_CONCURRENCY, ge=1)
    transcription_attempts: int = Field(default=DEFAULT_TRANSCRIPTION_ATTEMPTS, ge=1)
    transcription_retry_seconds: float = Field(default=DEFAULT_TRANSCRIPTION_RETRY_SECONDS, ge=0)
    transcription_grace_seconds: float = Field(default=DEFAULT_TRANSCRIPTION_GRACE_SECONDS, ge=0)
    # A detected page covering less of the still than this share may have been cropped wrong
    # (a box on the page taken for the sheet): the original still goes to Claude too.
    transcription_min_crop_share: float = Field(
        default=DEFAULT_TRANSCRIPTION_MIN_CROP_SHARE, ge=0, le=1
    )
    # Web search: off, no search is ever run. `web_search_role` is the `[llm.roles.<role>]` whose
    # client searches and fetches; one search runs at most `web_search_max_uses` searches and
    # offers at most `web_search_max_results` pages; a kept page is fetched with at most
    # `web_fetch_max_content_tokens` of content. With `web_auto_keep`, the pages Claude marks as
    # relevant are kept as sources at once, without waiting for the student.
    web_search_enabled: bool = True
    web_search_role: Literal["observer", "transcriber", "editor", "generator"] = (
        DEFAULT_WEB_SEARCH_ROLE
    )
    web_search_max_uses: int = Field(default=DEFAULT_WEB_SEARCH_MAX_USES, ge=1)
    web_search_max_results: int = Field(default=DEFAULT_WEB_SEARCH_MAX_RESULTS, ge=1, le=20)
    web_fetch_max_content_tokens: int = Field(default=DEFAULT_WEB_FETCH_MAX_CONTENT_TOKENS, ge=1000)
    web_search_concurrency: int = Field(default=DEFAULT_WEB_SEARCH_CONCURRENCY, ge=1)
    web_auto_keep: bool = False


# The live observer (`studentassistant.observer.live`): a batch goes to Claude once this many final
# segments, or this many seconds of speech, are waiting (a capture or a source switch: at once).
DEFAULT_OBSERVER_BATCH_SEGMENTS = 6
DEFAULT_OBSERVER_BATCH_SPEECH_SECONDS = 30.0
DEFAULT_OBSERVER_CATCH_UP_MAX_ITEMS = 200
DEFAULT_OBSERVER_CONTEXT_MAX_TOKENS = 80_000
DEFAULT_OBSERVER_CONTEXT_TAIL_SEGMENTS = 8


DEFAULT_MARP_COMMAND = ["marp"]
DEFAULT_MARP_TIMEOUT_SECONDS = 180.0
DEFAULT_GROUNDING_MIN_SUPPORT = 0.6


class GeneratorsSettings(BaseModel):
    """The study-material generators (`[generators]`, `SA_GENERATORS__*`)."""

    # The Marp CLI the slides generator exports PDF/PPTX with (`npm install -g
    # @marp-team/marp-cli`); a list, so `["npx", "--yes", "@marp-team/marp-cli"]` works too.
    marp_command: list[str] = Field(
        default_factory=lambda: list(DEFAULT_MARP_COMMAND), min_length=1
    )
    # How long one Marp export (PDF or PPTX) may take before it is abandoned.
    marp_timeout_seconds: float = Field(default=DEFAULT_MARP_TIMEOUT_SECONDS, gt=0)
    # The Chromium-based browser Marp renders with; unset, Marp finds one itself.
    marp_browser_path: Path | None = None
    # A generated item (a quiz answer, a flashcard's back...) whose share of content words found
    # in the note sections it cites is under this is reported as not grounded (never dropped).
    grounding_min_support: float = Field(default=DEFAULT_GROUNDING_MIN_SUPPORT, ge=0, le=1)


class ObserverSettings(BaseModel):
    """The live observer loop (`[observer]`, `SA_OBSERVER__*`)."""

    # Off: `serve` runs no observer and no observer call is ever made.
    enabled: bool = True
    batch_segments: int = Field(default=DEFAULT_OBSERVER_BATCH_SEGMENTS, ge=1)
    batch_speech_seconds: float = Field(default=DEFAULT_OBSERVER_BATCH_SPEECH_SECONDS, gt=0)
    # The most unanswered events a catch-up sends when a session opens (the newest are kept).
    catch_up_max_items: int = Field(default=DEFAULT_OBSERVER_CATCH_UP_MAX_ITEMS, ge=0)
    # Context purge (#60): once the conversation reaches this many tokens (the last call's prompt
    # plus its answer), it is rolled over to the snapshot + digest + the newest segments.
    context_max_tokens: int = Field(default=DEFAULT_OBSERVER_CONTEXT_MAX_TOKENS, ge=1)
    # How many of the newest answered segments the rolled-over conversation repeats.
    context_tail_segments: int = Field(default=DEFAULT_OBSERVER_CONTEXT_TAIL_SEGMENTS, ge=0)
    # The IANA zone (`Europe/Madrid`) the topic digest dates its sessions in; unset (or empty),
    # the PC's local zone.
    digest_timezone: str | None = None

    @field_validator("digest_timezone")
    @classmethod
    def check_digest_timezone(cls, name: str | None) -> str | None:
        if not name:
            return None
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(
                f"unknown timezone {name!r}: use an IANA name such as Europe/Madrid"
            ) from error
        return name

    def digest_zone(self) -> tzinfo:
        """The zone of `digest_timezone`, or the PC's local zone (`local_timezone`) when unset."""
        return ZoneInfo(self.digest_timezone) if self.digest_timezone else local_timezone()


def local_timezone() -> tzinfo:
    """The PC's local zone, with its DST rules: `TZ` when it names a known zone, else
    `/etc/localtime`, else UTC."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        with open("/etc/localtime", "rb") as file:
            return ZoneInfo.from_file(file, key="localtime")
    except (OSError, ValueError):
        return UTC


def config_toml_path() -> Path:
    """The TOML file to read: `SA_CONFIG` when it is set, the default location otherwise."""
    return Path(os.environ.get("SA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


# A configuration file `write_vault_config` creates is readable by its owner only.
NEW_CONFIG_MODE = 0o600


def write_vault_config(vault_path: Path, repo: str) -> bool:
    """Record `vault.path` and `vault.repo` in the TOML file at `config_toml_path()`.

    Every other key, table and comment already in the file is kept as it was (the file is edited,
    not regenerated), and nothing but these two keys is ever written, so no secret can reach it.
    The path is written absolute. The file keeps its permission bits (a new one is born `0600`),
    because the temporary file it is replaced by is created with them, never with the umask.
    Returns whether the file changed: writing the values it already holds leaves it untouched,
    modification time included.
    """
    check_repo_name(repo)
    target = config_toml_path()
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    document = tomlkit.parse(original)
    vault = document.get("vault")
    if vault is None:
        vault = tomlkit.table()
        document["vault"] = vault
    vault["path"] = str(vault_path.expanduser().resolve())
    vault["repo"] = repo
    updated = tomlkit.dumps(document)
    if updated == original:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else NEW_CONFIG_MODE
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.fchmod(descriptor, mode)  # `os.open` applies the umask to `mode`; undo that
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


DEFAULT_EVAL_SPEED = 4.0


class EvalSettings(BaseModel):
    """`studentassistant eval run`: where the eval set lives and how its sessions are replayed."""

    model_config = ConfigDict(validate_default=True)

    # One directory per case (recording + reference); every run's report goes to `runs/` in it.
    path: Path = DEFAULT_EVAL_PATH
    # How many times faster than recorded each session is replayed.
    speed: float = Field(default=DEFAULT_EVAL_SPEED, gt=0)

    @field_validator("path")
    @classmethod
    def expand_user(cls, path: Path) -> Path:
        return path.expanduser()


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
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    observer: ObserverSettings = Field(default_factory=ObserverSettings)
    eval: EvalSettings = Field(default_factory=EvalSettings)
    generators: GeneratorsSettings = Field(default_factory=GeneratorsSettings)

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
