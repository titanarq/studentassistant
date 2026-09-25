"""Preloading the `whisper` extra's CUDA wheels and the tiny cuBLAS/cuDNN check, without a GPU."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studentassistant.stt import cuda


@pytest.fixture(autouse=True)
def fresh_preload(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cuda, "_preloaded", None)
    yield


@pytest.fixture
def wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `nvidia` namespace package with the wheels' library files (not real ELF files)."""
    for package, names in cuda.WHEEL_LIBRARIES:
        lib = tmp_path.joinpath(*package.split("."), "lib")
        lib.mkdir(parents=True)
        for name in names:
            (lib / name).write_bytes(b"")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m == "nvidia" or m.startswith("nvidia.")]:
        monkeypatch.delitem(sys.modules, name)
    return tmp_path


class RecordingCDLL:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.opened: list[tuple[str, int]] = []

    def __call__(self, name: str, mode: int = 0) -> Any:
        self.opened.append((name, mode))
        if Path(name).name in self.fail:
            raise OSError(f"{name}: cannot open shared object file")
        return FakeLibrary()


class FakeLibrary:
    def __init__(self, status: int = 0) -> None:
        self.status = status

    def __getattr__(self, name: str) -> Any:
        def call(*_args: Any) -> int:
            return self.status

        return call


def test_wheel_library_dirs(wheels: Path) -> None:
    dirs = cuda.wheel_library_dirs()
    assert dirs == {
        "nvidia.cublas": wheels / "nvidia" / "cublas" / "lib",
        "nvidia.cudnn": wheels / "nvidia" / "cudnn" / "lib",
    }


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the wheels are Linux-only")
def test_preload_loads_the_wheels_globally_once(
    wheels: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdll = RecordingCDLL()
    monkeypatch.setattr(cuda.ctypes, "CDLL", cdll)

    loaded = cuda.preload()

    assert [p.name for p in loaded] == ["libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9"]
    assert all(mode == ctypes.RTLD_GLOBAL for _name, mode in cdll.opened)
    assert cuda.preload() == loaded
    assert len(cdll.opened) == 3


def test_preload_never_raises(wheels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda.ctypes, "CDLL", RecordingCDLL(fail={"libcublas.so.12"}))
    assert "libcublas.so.12" not in [p.name for p in cuda.preload()]


def test_preload_without_wheels_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda, "wheel_library_dirs", lambda: {})
    cdll = RecordingCDLL()
    monkeypatch.setattr(cuda.ctypes, "CDLL", cdll)
    assert cuda.preload() == []
    assert cdll.opened == []


def test_check_reports_a_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda, "wheel_library_dirs", lambda: {})
    monkeypatch.setattr(cuda.ctypes, "CDLL", RecordingCDLL(fail={"libcublas.so.12"}))
    assert cuda.check() == "no se encuentra libcublas.so.12 (cuBLAS de CUDA 12)"

    monkeypatch.setattr(cuda.ctypes, "CDLL", RecordingCDLL(fail={"libcudnn.so.9"}))
    assert cuda.check() == "no se encuentra libcudnn.so.9 (cuDNN 9 para CUDA 12)"


def test_check_creates_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda, "wheel_library_dirs", lambda: {})
    monkeypatch.setattr(cuda.ctypes, "CDLL", RecordingCDLL())
    assert cuda.check() is None

    monkeypatch.setattr(cuda.ctypes, "CDLL", lambda *_a, **_k: FakeLibrary(status=3))
    assert cuda.check() == "cuBLAS no arranca en la GPU (cublasCreate devolvió 3)"


@pytest.mark.integration
def test_check_on_this_gpu() -> None:
    """Needs the `whisper` extra and an NVIDIA GPU."""
    assert cuda.check() is None
