"""The only way a vault file reaches the disk: an atomic write and a deterministic YAML dump.

Both halves exist because the vault is a git repository that is the source of truth (ADR-0002).
Atomic, so a crash halfway through a write leaves the previous content in place instead of a
truncated file the student cannot recover. Deterministic, so the same model always produces the
same bytes: the student reads the vault's diffs on GitHub, and a dump whose key order or line
wrapping moved between two writes would drown the real change in noise.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from yaml import safe_dump, safe_load

from studentassistant.vault.models import VaultFileModel

# PyYAML wraps a scalar at 80 columns by default, which turns a one-word edit to a long
# `style_guide` into a change on every wrapped line of it. Nothing needs a vault file to be narrow.
_YAML_WIDTH = 1000


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader sees either the old content or all of the new one.

    The text lands in a temporary file in `path`'s own directory -- the same file system, which is
    what makes the rename atomic -- is flushed and fsynced there, and only then replaces the
    target; the directory is fsynced afterwards, because the rename is a directory entry and that
    entry is still in the page cache without it. What is written is exactly the bytes of `text`:
    the newlines are not translated. The result is private to the student's user (mode 0600, what
    `tempfile` creates), which is what notes and transcripts should be.

    `path`'s parent must already exist: making a directory is the caller's business, and a writer
    that silently made one would turn a misspelled slug into a stray tree nobody lists.

    Raises:
        FileNotFoundError: when `path`'s directory does not exist.
        OSError: when a write, an fsync or the rename fails; the temporary file is then removed and
            `path` keeps the content it had.
    """
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_yaml_atomic(path: Path, model: VaultFileModel) -> None:
    """Dump `model` as YAML and write it to `path` through `write_text_atomic`.

    The dump is `model_dump(mode="json")`, so the emitter only ever meets a plain scalar and a
    `datetime` is written as its ISO 8601 string instead of PyYAML's own timestamp format. Keys
    stay in the order the model declares them, which is the order the layout of
    `docs/modules/vault.md` shows them, and every declared key is written even when its value is
    `None`, so the file itself says what it can hold. No `---` or `...` marker is written: the
    first line of a vault file is its first field and the last one is its last field.
    """
    write_text_atomic(
        path,
        safe_dump(
            model.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=_YAML_WIDTH,
        ),
    )


def read_yaml[T: VaultFileModel](path: Path, model: type[T]) -> T:
    """Read the YAML at `path` and validate it into `model`.

    `safe_load` builds plain data and nothing else, so a file that came back from GitHub cannot
    execute anything on the way in; `model` then decides whether that data is a file this backend
    understands, and `VaultFileModel`'s `extra="forbid"` refuses a key it does not declare.

    Raises:
        FileNotFoundError: when `path` does not exist.
        ValidationError: when the file is empty or does not hold a `model`.
    """
    return model.model_validate(safe_load(path.read_text(encoding="utf-8")))


def _fsync_directory(directory: Path) -> None:
    """Fsync a directory, so a rename that just happened inside it survives the next crash."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
