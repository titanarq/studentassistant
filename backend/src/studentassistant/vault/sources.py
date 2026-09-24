"""Sources: what a topic's notes were built from, stored under `sources/{notes,book,pdf,web}/`.

Each stored source is two files: the content exactly as it was handed over, and a `.yaml` sidecar
holding its metadata (for a note page: capture id, session, capture time, transcript span; for a
web page: url, fetch time). Pages of `notes`, `book` and `pdf` are numbered `page-NNN.<ext>` in
the order they arrive; a web page is `NNN-<slug>.md`, its slug derived from the name it was given.
The next number is found by scanning the directory, so numbering resumes after whatever is there
already, including the derived files the `sources` module adds next to a page (`page-NNN.md`,
`page-NNN.page.jpg`).

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
from studentassistant.vault.files import dump_yaml, write_bytes_atomic, write_text_atomic
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
        extension = _extension_of(name)
        number = _next_number(directory, _PAGE_NUMBER)
        stem = f"page-{number:03d}"
        content_path = directory / f"{stem}{extension}"
    else:
        slug = slugify(name)
        number = _next_number(directory, _WEB_NUMBER)
        stem = f"{number:03d}-{slug}"
        content_path = directory / f"{stem}{WEB_SUFFIX}"

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
