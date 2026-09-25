"""The CUDA 12 libraries faster-whisper needs on the GPU (cuBLAS, cuDNN 9), from pip wheels.

CTranslate2 opens `libcublas.so.12` and `libcudnn*.so.9` by name only when the first model runs on
CUDA, so a PC with the NVIDIA driver but no CUDA toolkit sees the GPU and then fails on the first
encode. The optional `whisper` extra pulls the `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` wheels
(Linux) into the venv; `preload()` loads their libraries with `RTLD_GLOBAL` from the wheels'
site-packages directories, so CTranslate2's later lookup by name finds them already loaded and no
`LD_LIBRARY_PATH` is needed. Without the wheels it does nothing, and system-wide libraries (if
any) are found the usual way.

`check()` is the tiny CUDA test `doctor` runs: it opens each library by name, as CTranslate2 will,
and creates and destroys a cuBLAS and a cuDNN handle on the GPU. Nothing here imports CTranslate2
or runs at import time.
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# (wheel package, its libraries in load order); cuDNN's sub-libraries sit next to libcudnn.so.9,
# which finds them through its own `$ORIGIN` rpath.
WHEEL_LIBRARIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nvidia.cublas", ("libcublasLt.so.12", "libcublas.so.12")),
    ("nvidia.cudnn", ("libcudnn.so.9",)),
)
CUBLAS = "libcublas.so.12"
CUDNN = "libcudnn.so.9"

_lock = threading.Lock()
_preloaded: list[Path] | None = None


def wheel_library_dirs() -> dict[str, Path]:
    """`{package: <site-packages>/nvidia/<name>/lib}` for the NVIDIA wheels that are installed."""
    dirs: dict[str, Path] = {}
    for package, _names in WHEEL_LIBRARIES:
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ValueError):
            spec = None
        if spec is None or not spec.submodule_search_locations:
            continue
        for location in spec.submodule_search_locations:
            lib = Path(location) / "lib"
            if lib.is_dir():
                dirs[package] = lib
                break
    return dirs


def preload() -> list[Path]:
    """Load the wheels' CUDA libraries into the process (once); return the files loaded.

    Never raises: a library that does not load is logged and left for CTranslate2 to report."""
    global _preloaded
    with _lock:
        if _preloaded is not None:
            return list(_preloaded)
        loaded: list[Path] = []
        if sys.platform.startswith("linux"):
            dirs = wheel_library_dirs()
            for package, names in WHEEL_LIBRARIES:
                lib_dir = dirs.get(package)
                if lib_dir is None:
                    continue
                for name in names:
                    path = lib_dir / name
                    if not path.exists():
                        continue
                    try:
                        ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
                    except OSError as error:
                        logger.warning("could not preload %s: %s", path, error)
                        continue
                    loaded.append(path)
        if loaded:
            logger.info("preloaded CUDA libraries: %s", ", ".join(p.name for p in loaded))
        _preloaded = loaded
        return list(loaded)


def _handle_roundtrip(library: ctypes.CDLL, create: str, destroy: str) -> int:
    handle = ctypes.c_void_p()
    status = int(getattr(library, create)(ctypes.byref(handle)))
    if status == 0:
        getattr(library, destroy)(handle)
    return status


def check() -> str | None:
    """`None` when cuBLAS and cuDNN load and work on the GPU, else what failed (Spanish)."""
    preload()
    try:
        cublas = ctypes.CDLL(CUBLAS)
    except OSError:
        return f"no se encuentra {CUBLAS} (cuBLAS de CUDA 12)"
    try:
        cudnn = ctypes.CDLL(CUDNN)
    except OSError:
        return f"no se encuentra {CUDNN} (cuDNN 9 para CUDA 12)"
    try:
        status = _handle_roundtrip(cublas, "cublasCreate_v2", "cublasDestroy_v2")
        if status != 0:
            return f"cuBLAS no arranca en la GPU (cublasCreate devolvió {status})"
        status = _handle_roundtrip(cudnn, "cudnnCreate", "cudnnDestroy")
        if status != 0:
            return f"cuDNN no arranca en la GPU (cudnnCreate devolvió {status})"
    except (AttributeError, OSError) as error:
        return f"las bibliotecas de CUDA no responden: {error}"
    return None
