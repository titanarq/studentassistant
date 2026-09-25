"""Stand-ins for `faster_whisper` and `ctranslate2`, put in `sys.modules` by the tests."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


class FakeFasterWhisper(types.ModuleType):
    """`download_model` over a directory: "downloading" creates `<root>/<model>`."""

    def __init__(self, root: Path) -> None:
        super().__init__("faster_whisper")
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def download_model(
        self, size_or_id: str, cache_dir: str | None = None, local_files_only: bool = False
    ) -> str:
        self.calls.append(
            {"model": size_or_id, "cache_dir": cache_dir, "local_files_only": local_files_only}
        )
        if size_or_id == "bogus":
            raise ValueError(f"Invalid model size '{size_or_id}'")
        path = Path(cache_dir or self.root) / size_or_id
        if local_files_only and not path.exists():
            raise FileNotFoundError(str(path))
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, root: Path, cuda_devices: int | None = 1
) -> FakeFasterWhisper:
    fake = FakeFasterWhisper(root)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    if cuda_devices is None:
        monkeypatch.setitem(sys.modules, "ctranslate2", None)  # import raises ImportError
    else:
        ct2 = types.ModuleType("ctranslate2")
        ct2.get_cuda_device_count = lambda: cuda_devices  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
    return fake


def hide_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
