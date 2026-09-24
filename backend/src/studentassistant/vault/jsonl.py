"""Append-only JSONL: how a session's logs are written one line at a time and read back safely.

A log line is one compact JSON object ending in `\\n`, written with a single `write`, flushed and
fsynced before the append returns, so once `append_jsonl` returns the line survives a crash. What a
crash *during* an append can leave is a torn last line: bytes without their `\\n`. The reader
treats anything after the last `\\n` as not written yet and stops before it instead of raising, and
the next append cuts that torn tail off before writing, so a torn line never ends up in the middle
of a log. A complete line that is not JSON, or not the model asked for, is real corruption and is
refused with the file and line number.

`*.jsonl` files are merged by git with `merge=union` (ADR-0002), which keeps both sides' lines;
`last_seq` therefore reads the highest `seq` in the file rather than the last line's.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from studentassistant.vault.errors import VaultError
from studentassistant.vault.secrets import guard

_TAIL_CHUNK = 4096


class JsonlError(VaultError):
    """A complete line of a JSONL log is not JSON, or not the model it was read as."""


def encode_line(obj: BaseModel | Mapping[str, Any]) -> bytes:
    """The bytes `append_jsonl` writes for `obj`: compact JSON, UTF-8, one trailing `\\n`."""
    data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else dict(obj)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def append_jsonl(path: Path, obj: BaseModel | Mapping[str, Any]) -> None:
    """Append `obj` to the log at `path` as one line, and return only once it is on disk.

    The secret guard runs on the encoded line before the file is opened, so a refused line leaves
    the log (and, when it did not exist, the absence of one) exactly as it was. A torn last line a
    crash left behind is truncated away first. The file is created when missing; its directory
    must exist.

    Raises:
        SecretRefused: when the line looks like it carries an API key or a token.
        TypeError: when `obj` holds something JSON cannot encode.
        OSError: when the file cannot be opened, written or fsynced.
    """
    line = encode_line(obj)
    guard(line)
    with path.open("a+b") as log:
        size = log.seek(0, os.SEEK_END)
        if size:
            end = _end_of_last_complete_line(log, size)
            if end != size:
                log.truncate(end)
        log.write(line)
        log.flush()
        os.fsync(log.fileno())


def read_jsonl[T: BaseModel](path: Path, model: type[T]) -> Iterator[T]:
    """Yield every complete line of the log at `path`, parsed into `model`, in file order.

    Blank lines are skipped, and whatever follows the last `\\n` -- a line a crash tore, or nothing
    -- is not a line yet and is silently left out.

    Raises:
        FileNotFoundError: when there is no log at `path`.
        JsonlError: when a complete line is not JSON or does not hold a `model`.
    """
    for number, data in _complete_objects(path):
        try:
            yield model.model_validate(data)
        except ValidationError as error:
            raise JsonlError(
                f"{path}:{number} does not hold a {model.__name__}: {error}"
            ) from error


def last_seq(path: Path) -> int:
    """The highest `seq` among the complete lines of the log at `path`; 0 when it has none.

    A missing file has none either, which is what a log nobody appended to yet looks like.

    Raises:
        JsonlError: when a complete line is not a JSON object with an integer `seq`.
    """
    if not path.exists():
        return 0
    highest = 0
    for number, data in _complete_objects(path):
        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise JsonlError(f"{path}:{number} has no integer seq")
        highest = max(highest, seq)
    return highest


def _complete_objects(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Every complete, non-blank line of `path` as `(line number, JSON object)`."""
    content = path.read_bytes()
    lines = content.split(b"\n")
    # The last element is what follows the last `\n`: empty, or a torn line not written yet.
    for number, raw in enumerate(lines[:-1], start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JsonlError(f"{path}:{number} is not a JSON line: {error}") from error
        if not isinstance(data, dict):
            raise JsonlError(f"{path}:{number} is not a JSON object")
        yield number, data


def _end_of_last_complete_line(log: Any, size: int) -> int:
    """The offset just past the last `\\n` of an open binary file of `size` bytes (0 if none)."""
    position = size
    while position > 0:
        start = max(0, position - _TAIL_CHUNK)
        log.seek(start)
        chunk = log.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline != -1:
            return start + newline + 1
        position = start
    return 0
