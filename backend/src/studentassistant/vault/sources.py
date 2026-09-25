"""Sources: what a topic's notes were built from, stored under `sources/{notes,book,pdf,web}/`.

Each stored source is two files: the content exactly as it was handed over, and a `.yaml` sidecar
holding its metadata (for a note page: capture id, session, capture time, transcript span; for a
web page: url, fetch time). Pages of `notes`, `book` and `pdf` are numbered `page-NNN.<ext>` in
the order they arrive; a web page is `NNN-<slug>.md`, its slug derived from the name it was given.
The next number is found by scanning the directory, so numbering resumes after whatever is there
already, including the derived files the `sources` module adds next to a page (`page-NNN.md`,
`page-NNN.page.jpg`). Allocating a number and writing the files under it happen under one lock per
`sources/<kind>/` directory (`locking.py`), shared by every thread of the process and by every
other process on the vault (an `flock` under `.git/`), so two writers storing into the same topic
at once (a capture and a PDF upload, or the server and the CLI's `import-pdf`) get distinct
numbers and never overwrite each other.

Nothing here processes what it stores: the transcription of a page and the cropped page image are
derived by `sources`, which hands them back for storage. Both the content and the sidecar pass the
secret guard before either reaches the disk, so a refused source leaves neither file behind.

Reading is for callers that take a path from outside (the web read API): `list_sources` names each
stored source by its vault-relative path, and `read_source` accepts such a path only when it is
relative, has no `..`, names a file directly under a topic's `sources/<kind>/`, and still resolves
there once symlinks are followed. Nothing that reads writes a file or runs git.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from pydantic import TypeAdapter
from yaml import YAMLError, safe_load

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import (
    dump_yaml,
    read_yaml,
    write_bytes_atomic,
    write_text_atomic,
    write_yaml_atomic,
)
from studentassistant.vault.locking import directory_lock
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.secrets import guard
from studentassistant.vault.slugs import is_slug, slugify
from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.topics import TOPICS_DIRNAME, get_topic, require_topic, topic_directory
from studentassistant.vault.vault import Vault

SOURCES_DIRNAME = "sources"
SIDECAR_SUFFIX = ".yaml"
WEB_SUFFIX = ".md"

SourceKind = Literal["notes", "book", "pdf", "web"]
SOURCE_KINDS: tuple[str, ...] = get_args(SourceKind)
PAGED_KINDS: tuple[str, ...] = ("notes", "book", "pdf")

_PAGE_NUMBER = re.compile(r"^page-(\d{3,})\.")
_WEB_NUMBER = re.compile(r"^(\d{3,})-")
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]+$")
_DERIVED_SUFFIX = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)+$")
_META_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])

# How long a writer waits for another process (or thread) to finish storing into the same
# `sources/<kind>/` directory before giving up with `VaultBusyError`.
SOURCE_LOCK_TIMEOUT_SECONDS = 120.0


class SourceError(VaultError):
    """A source this backend refuses to store; the message says why."""


class UnknownSourceKindError(SourceError):
    """The `kind` is not one of `notes`, `book`, `pdf`, `web`."""


class SourcePathError(SourceError):
    """A path `read_source` refuses to follow: absolute, with `..`, outside a topic's `sources/`
    or resolving (symlinks included) out of it."""


class SourceNotFoundError(SourceError):
    """A well-formed source path under which there is no file."""


class SourceFileError(SourceError):
    """A source's `.yaml` sidecar exists but is not readable as a YAML mapping."""


@dataclass(frozen=True)
class StoredSource:
    """One stored source as `list_sources` lists it.

    `path` is the content's vault-relative POSIX path (what `read_source` takes), `meta` its parsed
    sidecar, or `None` when it has none.
    """

    kind: str
    path: str
    meta: dict[str, Any] | None


@dataclass(frozen=True)
class SourceContent:
    """What `read_source` returns: the bytes, the sidecar metadata and a guessed media type."""

    content: bytes
    meta: dict[str, Any] | None
    media_type: str


def sources_directory(vault: Vault, subject_slug: str, topic_slug: str, kind: str) -> Path:
    """Where a topic keeps the sources of one kind, whether or not it has any yet."""
    return topic_directory(vault, subject_slug, topic_slug) / SOURCES_DIRNAME / kind


def put_source(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    kind: str,
    name: str,
    content: bytes | str,
    meta: Mapping[str, Any],
    derived: Mapping[str, bytes | str] | None = None,
) -> Path:
    """Store one source of a topic with its `.yaml` metadata sidecar and return the content's path.

    For `notes`, `book` and `pdf`, `name` is the file the content came as, and only its extension
    is kept (`foto.JPG` -> `page-004.jpg`). For `web`, `name` is the page's title and becomes the
    slug of `NNN-<slug>.md`. `content` is written as it is: bytes untouched, text as UTF-8. `meta`
    is dumped with the same deterministic YAML as every vault file; datetimes become ISO 8601.

    `derived` (paged kinds only) maps name suffixes to files `sources` derived from the content,
    written next to it as `page-NNN.<suffix>` in the same call: `{"p003.txt": ..., "p003.jpg":
    ...}` gives `page-NNN.p003.txt` and `page-NNN.p003.jpg` (a PDF's per-page text and image).
    A suffix is dot-separated lowercase letters and digits with at least one dot, so a derived
    file never lists as a source of its own nor clashes with the content or its sidecar. All of
    them pass the secret guard before anything is written, and a failure removes whatever of the
    source was already written.

    Raises:
        UnknownSourceKindError: when `kind` is not a source kind; nothing is written.
        SourceError: when a paged source's `name` has no usable extension, or a `derived`
            suffix is malformed or given for a web source.
        ValueError: when a web source's `name` has no letter or digit to slug.
        SecretRefused: when the content or the metadata looks like it carries a key; neither file
            is written.
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read.
        VaultBusyError: when another process kept the directory locked for longer than
            `SOURCE_LOCK_TIMEOUT_SECONDS`; nothing is written.
    """
    if kind not in SOURCE_KINDS:
        raise UnknownSourceKindError(
            f"{kind!r} is not a source kind; the kinds are {', '.join(SOURCE_KINDS)}"
        )
    get_topic(vault, subject_slug, topic_slug)
    directory = sources_directory(vault, subject_slug, topic_slug, kind)
    sidecar_text = dump_yaml(_META_ADAPTER.dump_python(dict(meta), mode="json"))
    guard(content)
    guard(sidecar_text)
    derived_files = dict(derived or {})
    if derived_files and kind not in PAGED_KINDS:
        raise SourceError(f"a {kind} source has no derived files")
    for suffix, derived_content in derived_files.items():
        if not _DERIVED_SUFFIX.match(suffix):
            raise SourceError(
                f"{suffix!r} is not a derived-file suffix (dot-separated lowercase letters"
                " and digits with at least one dot, e.g. p003.txt)"
            )
        guard(derived_content)

    if kind in PAGED_KINDS:
        pattern, stem_template, content_suffix = _PAGE_NUMBER, "page-{:03d}", _extension_of(name)
    else:
        pattern, stem_template, content_suffix = (
            _WEB_NUMBER,
            f"{{:03d}}-{slugify(name)}",
            WEB_SUFFIX,
        )

    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        # Scanning for the next number and writing under it is one step for every writer of the
        # vault; outside the lock, two writers could both see the same highest number.
        stem = stem_template.format(_next_number(directory, pattern))
        return _store(directory, stem, content_suffix, content, sidecar_text, derived_files)


def _store(
    directory: Path,
    stem: str,
    content_suffix: str,
    content: bytes | str,
    sidecar_text: str,
    derived_files: Mapping[str, bytes | str],
) -> Path:
    """Write the content, its derived files and last its sidecar under `stem`; all or nothing."""
    content_path = directory / f"{stem}{content_suffix}"
    directory.mkdir(parents=True, exist_ok=True)
    sidecar_path = directory / f"{stem}{SIDECAR_SUFFIX}"
    written: list[Path] = []
    try:
        for path, data in (
            (content_path, content),
            *((directory / f"{stem}.{suffix}", data) for suffix, data in derived_files.items()),
        ):
            _write(path, data)
            written.append(path)
        write_text_atomic(sidecar_path, sidecar_text)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return content_path


def _write(path: Path, data: bytes | str) -> None:
    if isinstance(data, bytes):
        write_bytes_atomic(path, data)
    else:
        write_text_atomic(path, data)


def _extension_of(name: str) -> str:
    extension = Path(name).suffix.lower()
    if not _EXTENSION.match(extension):
        raise SourceError(
            f"cannot store {name!r} as a page: it has no file extension to keep"
            " (page-NNN.<ext> needs one, e.g. .jpg)"
        )
    return extension


def _next_number(directory: Path, pattern: re.Pattern[str]) -> int:
    """One past the highest number any entry of `directory` carries under `pattern`; 1 if none."""
    if not directory.is_dir():
        return 1
    numbers = [
        int(match.group(1))
        for entry in directory.iterdir()
        if (match := pattern.match(entry.name)) is not None
    ]
    return max(numbers, default=0) + 1


TRANSCRIPTION_SUFFIX = ".md"


def put_page_transcription(vault: Vault, vault_relative_path: str, text: str) -> Path:
    """Write the Markdown transcription of a stored page as `page-NNN.md` next to it.

    `vault_relative_path` names the page as `list_sources` does (or any file derived from it,
    such as its `page-NNN.page.jpg`): a file under a topic's `sources/notes|book|pdf/` whose name
    starts with `page-NNN.`. The text passes the secret guard and is written atomically as UTF-8;
    a transcription already there is replaced (the page was transcribed again). Returns the path
    written.

    Raises:
        SourcePathError: when the path is not a page of a paged kind (see `read_source`).
        SourceNotFoundError: when the page's sidecar (`page-NNN.yaml`) is not there.
        SecretRefused: when the text looks like it carries a key; nothing is written.
    """
    parts = _checked_parts(vault_relative_path)
    if parts[5] not in PAGED_KINDS:
        raise SourcePathError(f"{vault_relative_path!r} is not a page of a paged source kind")
    match = _PAGE_NUMBER.match(parts[-1])
    if match is None:
        raise SourcePathError(f"{vault_relative_path!r} does not name a page-NNN file")
    directory = vault.path.joinpath(*parts[:-1])
    stem = parts[-1].split(".", 1)[0]
    sidecar = directory / f"{stem}{SIDECAR_SUFFIX}"
    if directory.resolve() != vault.path.resolve().joinpath(*parts[:-1]):
        raise SourcePathError(
            f"{vault_relative_path!r} goes through a symlink out of its sources directory"
        )
    if not sidecar.is_file() or sidecar.is_symlink():
        raise SourceNotFoundError(f"there is no stored page {stem} at {vault_relative_path!r}")
    guard(text)
    target = directory / f"{stem}{TRANSCRIPTION_SUFFIX}"
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        write_text_atomic(target, text)
    return target


def update_page_meta(vault: Vault, vault_relative_path: str, updates: Mapping[str, Any]) -> Path:
    """Merge `updates` into the sidecar (`page-NNN.yaml`) of a stored page and return its path.

    For what `sources` learns about a page after it was stored (a textbook page's printed page
    number, found by its transcription). `vault_relative_path` names the page or a file derived
    from it, as for `put_page_transcription`. The keys of `updates` replace the sidecar's own of
    the same name (a key set to `None` is written as `null`); every other key is kept, in its
    order. The result passes the secret guard and is written atomically under the directory's
    lock, so it never interleaves with a store into the same directory.

    Raises:
        SourcePathError: when the path is not a page of a paged kind.
        SourceNotFoundError: when the page's sidecar is not there.
        SourceFileError: when the sidecar is not a readable YAML mapping.
        SecretRefused: when the merged metadata looks like it carries a key; nothing is written.
    """
    parts = _checked_parts(vault_relative_path)
    if parts[5] not in PAGED_KINDS or _PAGE_NUMBER.match(parts[-1]) is None:
        raise SourcePathError(f"{vault_relative_path!r} is not a page of a paged source kind")
    directory = vault.path.joinpath(*parts[:-1])
    if directory.resolve() != vault.path.resolve().joinpath(*parts[:-1]):
        raise SourcePathError(
            f"{vault_relative_path!r} goes through a symlink out of its sources directory"
        )
    sidecar = directory / f"{parts[-1].split('.', 1)[0]}{SIDECAR_SUFFIX}"
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        if not sidecar.is_file() or sidecar.is_symlink():
            raise SourceNotFoundError(f"there is no stored page at {vault_relative_path!r}")
        meta = _read_sidecar(sidecar) or {}
        meta.update(updates)
        text = dump_yaml(_META_ADAPTER.dump_python(meta, mode="json"))
        guard(text)
        write_text_atomic(sidecar, text)
    return sidecar


# -- the textbook of a topic -----------------------------------------------------------------------

BOOK_FILE_NAME = "book.yaml"
"""`sources/book/book.yaml`: which textbook the topic's `book` pages come from."""


class Book(VaultFileModel):
    """`sources/book/book.yaml`: the textbook a topic's book pages are photographed from."""

    title: str


def book_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    return sources_directory(vault, subject_slug, topic_slug, "book") / BOOK_FILE_NAME


def get_book(vault: Vault, subject_slug: str, topic_slug: str) -> Book | None:
    """The topic's textbook, `None` when none was set. Nothing is written.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: as
            `require_topic`.
        SourceFileError: when `book.yaml` is there but is not a `Book`.
    """
    require_topic(vault, subject_slug, topic_slug)
    path = book_path(vault, subject_slug, topic_slug)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return read_yaml(path, Book)
    except (OSError, UnicodeDecodeError, YAMLError, ValueError) as error:
        raise SourceFileError(f"{path} is not a book this backend can read: {error}") from error


def set_book(vault: Vault, subject_slug: str, topic_slug: str, title: str) -> Book:
    """Record the topic's textbook (`title`, stripped) in `sources/book/book.yaml`.

    `book.yaml` is never listed as a source nor numbered as a page. Returns the book as written
    (the file is not rewritten when it already holds that title).

    Raises:
        ValueError: `title` is empty once stripped; nothing is written.
        SecretRefused: the title looks like a key; nothing is written.
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: as
            `require_topic`.
    """
    cleaned = " ".join(title.split())
    if not cleaned:
        raise ValueError("a book needs a title")
    book = Book(title=cleaned)
    current = get_book(vault, subject_slug, topic_slug)
    if current == book:
        return book
    directory = sources_directory(vault, subject_slug, topic_slug, "book")
    guard(cleaned)
    directory.mkdir(parents=True, exist_ok=True)
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        write_yaml_atomic(directory / BOOK_FILE_NAME, book)
    return book


# -- reading ---------------------------------------------------------------------------------------

_PAGE_SOURCE = re.compile(r"^page-(\d{3,})\.([A-Za-z0-9]+)$")
_WEB_SOURCE = re.compile(r"^(\d{3,})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_DERIVED_TRANSCRIPTION = "md"
_MEDIA_TYPES = mimetypes.MimeTypes()  # built-in table only: no host file makes it differ
_MEDIA_TYPE_OVERRIDES = {".md": "text/markdown", ".yaml": "application/yaml"}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"
# subjects/<subject>/topics/<topic>/sources/<kind>/<file>
_SOURCE_PATH_PARTS = 7


def list_sources(vault: Vault, subject_slug: str, topic_slug: str) -> list[StoredSource]:
    """Every stored source of the topic, ordered by kind (`SOURCE_KINDS` order) then number.

    A source is the content `put_source` stored: `page-NNN.<ext>` under `notes`, `book` and `pdf`,
    `NNN-<slug>.md` under `web`. Sidecars (`.yaml`) and the derived files `sources` adds next to a
    page (`page-NNN.md`, `page-NNN.page.jpg`) are not sources of their own; a `page-NNN.md` counts
    as the source only when no other `page-NNN.<ext>` is there. Symlinks are never listed. A topic
    without `sources/` lists as empty. Nothing is written.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read (a value that is not a slug is not found).
        SourceFileError: when a listed source's sidecar is not a readable YAML mapping.
    """
    require_topic(vault, subject_slug, topic_slug)
    listed: list[StoredSource] = []
    for kind in SOURCE_KINDS:
        directory = sources_directory(vault, subject_slug, topic_slug, kind)
        if not directory.is_dir():
            continue
        entries = sorted(_source_entries(directory, kind), key=lambda item: (item[0], item[1].name))
        for _, entry in entries:
            listed.append(
                StoredSource(
                    kind=kind,
                    path=entry.relative_to(vault.path).as_posix(),
                    meta=_read_sidecar(_sidecar_of(entry)),
                )
            )
    return listed


def read_source(vault: Vault, vault_relative_path: str) -> SourceContent:
    """The bytes of one file under a topic's `sources/<kind>/`, its sidecar and its media type.

    `vault_relative_path` is taken literally (nothing is URL-decoded) and must be a relative POSIX
    path `subjects/<subject>/topics/<topic>/sources/<kind>/<file>` with slugs, a source kind and no
    `.`/`..`/empty segment; the file, once symlinks are resolved, must still be inside that same
    `sources/<kind>/` directory of the vault. Derived files (`page-NNN.md`, `page-NNN.page.jpg`)
    are readable too and come with their page's sidecar; a sidecar read directly has none. The
    media type is guessed from the extension (`application/octet-stream` when unknown). Nothing is
    written.

    Raises:
        SourcePathError: when the path is not of that form, or resolves anywhere else.
        SourceNotFoundError: when there is no file at that path.
        SourceFileError: when the sidecar is not a readable YAML mapping.
    """
    parts = _checked_parts(vault_relative_path)
    directory = vault.path.joinpath(*parts[:-1])
    target = directory / parts[-1]
    try:
        root = vault.path.resolve()
        allowed = directory.resolve()
        resolved = target.resolve()
    except (OSError, RuntimeError) as error:
        raise SourcePathError(f"{vault_relative_path!r} cannot be resolved: {error}") from error
    if allowed != root.joinpath(*parts[:-1]):
        raise SourcePathError(
            f"{vault_relative_path!r} goes through a symlink out of its sources directory"
        )
    if resolved.parent != allowed:
        raise SourcePathError(f"{vault_relative_path!r} resolves outside its sources directory")
    if not resolved.is_file():
        raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}")
    try:
        content = resolved.read_bytes()
    except FileNotFoundError as error:
        raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}") from error
    meta = None
    if target.suffix != SIDECAR_SUFFIX:
        meta = _read_sidecar(_sidecar_of(target))
    return SourceContent(content=content, meta=meta, media_type=_media_type(target))


def _source_entries(directory: Path, kind: str) -> list[tuple[int, Path]]:
    """`(number, path)` of each source content file in one kind's directory."""
    entries = [entry for entry in directory.iterdir() if not entry.is_symlink() and entry.is_file()]
    if kind not in PAGED_KINDS:
        return [
            (int(match.group(1)), entry)
            for entry in entries
            if (match := _WEB_SOURCE.match(entry.name)) is not None
        ]
    by_number: dict[int, list[tuple[str, Path]]] = {}
    for entry in entries:
        match = _PAGE_SOURCE.match(entry.name)
        if match is None or f".{match.group(2)}" == SIDECAR_SUFFIX:
            continue
        by_number.setdefault(int(match.group(1)), []).append((match.group(2).lower(), entry))
    chosen: list[tuple[int, Path]] = []
    for number, candidates in by_number.items():
        originals = [entry for ext, entry in candidates if ext != _DERIVED_TRANSCRIPTION]
        chosen.extend((number, entry) for entry in originals or [e for _, e in candidates])
    return chosen


def _checked_parts(vault_relative_path: str) -> tuple[str, ...]:
    """The segments of a source path, refused unless it has exactly the shape a source has."""
    if not isinstance(vault_relative_path, str) or not vault_relative_path:
        raise SourcePathError("a source path must be a non-empty string")
    if "\x00" in vault_relative_path or "\\" in vault_relative_path:
        raise SourcePathError(f"{vault_relative_path!r} holds a character no source path has")
    if vault_relative_path.startswith("/") or PurePosixPath(vault_relative_path).is_absolute():
        raise SourcePathError(f"{vault_relative_path!r} is absolute; a source path is relative")
    parts = tuple(vault_relative_path.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise SourcePathError(f"{vault_relative_path!r} has an empty, '.' or '..' segment")
    if (
        len(parts) != _SOURCE_PATH_PARTS
        or parts[0] != SUBJECTS_DIRNAME
        or not is_slug(parts[1])
        or parts[2] != TOPICS_DIRNAME
        or not is_slug(parts[3])
        or parts[4] != SOURCES_DIRNAME
        or parts[5] not in SOURCE_KINDS
    ):
        raise SourcePathError(
            f"{vault_relative_path!r} is not a file under a topic's sources/<kind>/ directory"
        )
    return parts


def _sidecar_of(content_path: Path) -> Path:
    """The sidecar a content or derived file belongs to: `page-001.page.jpg` -> `page-001.yaml`."""
    stem = content_path.name.split(".", 1)[0]
    return content_path.with_name(f"{stem}{SIDECAR_SUFFIX}")


def _read_sidecar(sidecar: Path) -> dict[str, Any] | None:
    """The sidecar's mapping; `None` when there is none or it is a symlink (never followed)."""
    if sidecar.is_symlink():
        return None
    try:
        text = sidecar.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise SourceFileError(f"{sidecar} cannot be read: {error}") from error
    try:
        data = safe_load(text)
    except YAMLError as error:
        raise SourceFileError(f"{sidecar} is not YAML this backend can parse: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SourceFileError(f"{sidecar} does not hold a mapping of metadata")
    return data


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MEDIA_TYPE_OVERRIDES:
        return _MEDIA_TYPE_OVERRIDES[suffix]
    guessed, _ = _MEDIA_TYPES.guess_type(path.name, strict=False)
    return guessed or _DEFAULT_MEDIA_TYPE
