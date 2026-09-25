"""Preparing and checking the faster-whisper model, when `stt.provider = "faster-whisper"`.

Only used in server STT mode with faster-whisper selected (ADR-0008); in the default client mode
the capture client transcribes and there is nothing to download. faster-whisper is an optional
dependency: it and `ctranslate2` are imported only when asked for, so the rest of the backend
never needs them. Options come from `[stt.options.faster-whisper]` (`model`, `device`,
`download_root`), with the defaults in `studentassistant.config`.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from studentassistant.config import DEFAULT_WHISPER_DEVICE, DEFAULT_WHISPER_MODEL, SttSettings

PROVIDER_NAME = "faster-whisper"
NOT_INSTALLED_MESSAGE = (
    "faster-whisper no está instalado en el entorno de Student Assistant"
    " (instálalo en backend/ con `uv pip install faster-whisper`)"
)


class WhisperError(Exception):
    """The model could not be prepared or checked; the message is Spanish and safe to show."""


@dataclass(frozen=True)
class WhisperOptions:
    model: str
    device: str
    download_root: str | None


def uses_whisper(settings: SttSettings) -> bool:
    """Whether the configuration transcribes on this PC with faster-whisper."""
    return settings.mode == "server" and settings.provider == PROVIDER_NAME


def whisper_options(settings: SttSettings) -> WhisperOptions:
    options: dict[str, Any] = settings.provider_options(PROVIDER_NAME)
    root = options.get("download_root")
    return WhisperOptions(
        model=str(options.get("model") or DEFAULT_WHISPER_MODEL),
        device=str(options.get("device") or DEFAULT_WHISPER_DEVICE),
        download_root=str(Path(root).expanduser()) if root else None,
    )


def _import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def faster_whisper_module() -> ModuleType:
    module = _import("faster_whisper")
    if module is None:
        raise WhisperError(NOT_INSTALLED_MESSAGE)
    return module


def download(options: WhisperOptions) -> Path:
    """Download the model into the cache (a no-op when it is already there); return its path."""
    module = faster_whisper_module()
    try:
        path = module.download_model(options.model, cache_dir=options.download_root)
    except Exception as error:  # ValueError for a bad size, huggingface_hub/OS errors otherwise
        raise WhisperError(f"no se pudo descargar el modelo {options.model}: {error}") from error
    return Path(path)


def cached_model(options: WhisperOptions) -> Path:
    """The model's local path, without downloading; `WhisperError` when it is not cached."""
    module = faster_whisper_module()
    try:
        path = module.download_model(
            options.model, cache_dir=options.download_root, local_files_only=True
        )
    except Exception as error:
        raise WhisperError(
            f"el modelo {options.model} no está descargado: lanza `studentassistant setup`"
        ) from error
    return Path(path)


def cuda_devices() -> int | None:
    """How many CUDA devices CTranslate2 sees, or `None` when it cannot tell (not installed)."""
    module = _import("ctranslate2")
    if module is None:
        return None
    try:
        return int(module.get_cuda_device_count())
    except Exception:
        return 0
