"""The faster-whisper model: options with config defaults, download, cached lookup, CUDA."""

from __future__ import annotations

from pathlib import Path

import pytest
from whisper_fakes import hide_faster_whisper, install_fakes

from studentassistant.config import DEFAULT_WHISPER_DEVICE, DEFAULT_WHISPER_MODEL, SttSettings
from studentassistant.install import whisper


def whisper_settings(**options: object) -> SttSettings:
    return SttSettings(
        mode="server", provider="faster-whisper", options={"faster-whisper": options}
    )


def test_only_server_mode_with_faster_whisper_uses_it() -> None:
    assert whisper.uses_whisper(whisper_settings())
    assert not whisper.uses_whisper(SttSettings())
    assert not whisper.uses_whisper(SttSettings(mode="client", provider="faster-whisper"))
    assert not whisper.uses_whisper(SttSettings(mode="server", provider="fake"))


def test_options_default_from_the_config(tmp_path: Path) -> None:
    assert whisper.whisper_options(whisper_settings()) == whisper.WhisperOptions(
        DEFAULT_WHISPER_MODEL, DEFAULT_WHISPER_DEVICE, None
    )
    chosen = whisper.whisper_options(
        whisper_settings(model="small", device="cpu", download_root=str(tmp_path))
    )
    assert chosen == whisper.WhisperOptions("small", "cpu", str(tmp_path))


def test_download_then_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = install_fakes(monkeypatch, tmp_path / "hf")
    options = whisper.WhisperOptions("small", "auto", str(tmp_path / "models"))

    with pytest.raises(whisper.WhisperError, match="no está descargado"):
        whisper.cached_model(options)
    path = whisper.download(options)

    assert path == tmp_path / "models" / "small"
    assert whisper.cached_model(options) == path
    assert fake.calls[1] == {
        "model": "small",
        "cache_dir": str(tmp_path / "models"),
        "local_files_only": False,
    }


def test_a_bad_model_is_a_spanish_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fakes(monkeypatch, tmp_path)
    with pytest.raises(whisper.WhisperError, match="no se pudo descargar el modelo bogus"):
        whisper.download(whisper.WhisperOptions("bogus", "auto", None))


def test_missing_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_faster_whisper(monkeypatch)
    with pytest.raises(whisper.WhisperError, match="faster-whisper no está instalado"):
        whisper.download(whisper.WhisperOptions("small", "auto", None))


def test_cuda_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fakes(monkeypatch, tmp_path, cuda_devices=2)
    assert whisper.cuda_devices() == 2
    install_fakes(monkeypatch, tmp_path, cuda_devices=None)
    assert whisper.cuda_devices() is None
