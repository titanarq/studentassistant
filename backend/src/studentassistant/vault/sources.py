"""Sources: what a topic's notes were built from, stored under `sources/<kind>/`.

Each stored source is two files: the content exactly as it was handed over, and a `.yaml` sidecar
holding its metadata (for a note page: capture id, session, capture time, transcript span; for a
web page: url, fetch time). Pages of `notes`, `book` and `pdf` are numbered `page-NNN.<ext>` in
the order they arrive; a web page is `NNN-<slug>.md`, its slug derived from the name it was given;
an image the student pasted into the notes is `images/img-NNN.<png|jpg|webp>`, a diagram the
editor drew `images/img-NNN.svg` (sanitized before it is written, `svg.py`).
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

Sources are never deleted from the working tree by the student (#451): `remove_source` *retires*
one, a soft delete that writes a `removed` mapping into its sidecar. A removed source keeps every
file (and its git history), `read_source` still serves it -- so a footnote of the notes that cites
it keeps resolving --, but `list_sources` leaves it out unless asked, and with it every caller that
builds a listing, a catalogue or a context from that list.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from pydantic import TypeAdapter, ValidationError
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
from studentassistant.vault.svg import SVG_EXTENSION, sanitize_svg
from studentassistant.vault.topics import TOPICS_DIRNAME, get_topic, require_topic, topic_directory
from studentassistant.vault.vault import Vault

SOURCES_DIRNAME = "sources"
SIDECAR_SUFFIX = ".yaml"
WEB_SUFFIX = ".md"

SourceKind = Literal["notes", "book", "pdf", "web", "images"]
SOURCE_KINDS: tuple[str, ...] = get_args(SourceKind)
PAGED_KINDS: tuple[str, ...] = ("notes", "book", "pdf")
IMAGES_KIND = "images"
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
"""The media types a pasted image may have, and the extension each is stored with."""

_PAGE_NUMBER = re.compile(r"^page-(\d{3,})\.")
_WEB_NUMBER = re.compile(r"^(\d{3,})-")
_IMAGE_NUMBER = re.compile(r"^img-(\d{3,})\.")
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]+$")
_DERIVED_SUFFIX = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)+$")
_META_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
# A sidecar's `added_at`, as YAML gives it back (an ISO 8601 string or a datetime).
_INSTANT: TypeAdapter[datetime] = TypeAdapter(datetime)

# How long a writer waits for another process (or thread) to finish storing into the same
# `sources/<kind>/` directory before giving up with `VaultBusyError`.
SOURCE_LOCK_TIMEOUT_SECONDS = 120.0


class SourceError(VaultError):
    """A source this backend refuses to store; the message says why."""


class UnknownSourceKindError(SourceError):
    """The `kind` is not one of `notes`, `book`, `pdf`, `web`, `images`."""


class SourcePathError(SourceError):
    """A path `read_source` refuses to follow: absolute, with `..`, outside a topic's `sources/`
    or resolving (symlinks included) out of it."""


class SourceNotFoundError(SourceError):
    """A well-formed source path under which there is no file."""


class SourceFileError(SourceError):
    """A source's `.yaml` sidecar exists but is not readable as a YAML mapping."""


REMOVED_KEY = "removed"
"""The sidecar key `remove_source` writes: `{at: <ISO 8601>, by: student}` (#451)."""


def is_removed(meta: Mapping[str, Any] | None) -> bool:
    """Whether a sidecar marks its source as removed (a soft delete, `remove_source`)."""
    return bool(meta) and isinstance(meta.get(REMOVED_KEY), Mapping)  # type: ignore[union-attr]


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

    For `notes`, `book` and `pdf`, `name` is the file the content came as, and only its extension is
    kept (`foto.JPG` -> `page-004.jpg`); for `images` the same gives `img-NNN.<ext>`, the extension
    one of `.png`, `.jpg` (`.jpeg` too), `.webp` or `.svg` -- an SVG (a diagram the editor drew,
    #511) is stored as `svg.sanitize_svg` rebuilds it, never as given. For `web`, `name` is the
    page's title and becomes the slug of `NNN-<slug>.md`. `content` is written as it is: bytes
    untouched, text as UTF-8. `meta` is dumped with the same deterministic YAML as every vault
    file; datetimes become ISO 8601.

    `derived` (paged kinds only) maps name suffixes to files `sources` derived from the content,
    written next to it as `page-NNN.<suffix>` in the same call: `{"p003.txt": ..., "p003.jpg":
    ...}` gives `page-NNN.p003.txt` and `page-NNN.p003.jpg` (a PDF's per-page text and image).
    A suffix is dot-separated lowercase letters and digits with at least one dot, so a derived
    file never lists as a source of its own nor clashes with the content or its sidecar. All of
    them pass the secret guard before anything is written, and a failure removes whatever of the
    source was already written.

    Raises:
        UnknownSourceKindError: when `kind` is not a source kind; nothing is written.
        SvgError: when an `.svg` image is not a drawing `sanitize_svg` can keep; nothing is
            written.
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
    if kind == IMAGES_KIND and _image_extension(name) == SVG_EXTENSION:
        # An SVG is drawn by the editor (#511): only its sanitized form is ever stored.
        content = sanitize_svg(content)
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
    elif kind == IMAGES_KIND:
        pattern, stem_template, content_suffix = _IMAGE_NUMBER, "img-{:03d}", _image_extension(name)
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


def _image_extension(name: str) -> str:
    extension = Path(name).suffix.lower()
    extension = ".jpg" if extension == ".jpeg" else extension
    if extension not in (*IMAGE_EXTENSIONS.values(), SVG_EXTENSION):
        raise SourceError(
            f"cannot store {name!r} as an image: only .png, .jpg, .webp and .svg images are kept"
        )
    return extension


def put_pasted_image(
    vault: Vault,
    subject_slug: str,
    topic_slug: str,
    content: bytes,
    content_type: str,
    *,
    added_at: datetime | None = None,
) -> Path:
    """Store an image the student pasted into the notes as `sources/images/img-NNN.<ext>`.

    The extension follows `content_type` (`IMAGE_EXTENSIONS`); the sidecar records `origin:
    pasted`, the `content_type`, the content's `sha256` and `added_at` (now, UTC, by default).
    Returns the content's path. Raises what `put_source` raises, and `SourceError` for a media
    type that is not a PNG, JPEG or WebP image or an empty content; nothing is written then.
    """
    extension = IMAGE_EXTENSIONS.get(content_type)
    if extension is None:
        raise SourceError(f"{content_type!r} is not an image type this vault keeps")
    if not content:
        raise SourceError("an empty image is not stored")
    meta = {
        "origin": "pasted",
        "content_type": content_type,
        "sha256": hashlib.sha256(content).hexdigest(),
        "added_at": added_at or datetime.now(UTC),
    }
    return put_source(
        vault, subject_slug, topic_slug, IMAGES_KIND, f"pasted{extension}", content, meta
    )


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


def put_page_transcription(
    vault: Vault, vault_relative_path: str, text: str, *, page: int | None = None
) -> Path:
    """Write the Markdown transcription of a stored page as `page-NNN.md` next to it.

    `vault_relative_path` names the page as `list_sources` does (or any file derived from it,
    such as its `page-NNN.page.jpg`): a file under a topic's `sources/notes|book|pdf/` whose name
    starts with `page-NNN.`. With `page` (page `K` of a stored PDF, from 1), the file written is
    `page-NNN.pKKK.md` instead: the transcription of one scanned page of that PDF. The text
    passes the secret guard and is written atomically as UTF-8; a transcription already there is
    replaced (the page was transcribed again). Returns the path written.

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
    if page is not None and page < 1:
        raise SourcePathError(f"page {page} of {vault_relative_path!r} is not a page number")
    guard(text)
    part = "" if page is None else f".p{page:03d}"
    target = directory / f"{stem}{part}{TRANSCRIPTION_SUFFIX}"
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


TRANSCRIPTION_EDITED_KEY = "transcription_edited"
"""The sidecar key `edit_page_transcription` writes (#473).

`{at, by: student, previous_sha256, original_sha256}`: when, who, and the hashes of the text the
last correction replaced and of the one before the first correction (the machine's).
"""
EDITABLE_TRANSCRIPTION_KINDS: tuple[str, ...] = ("notes", "book")
"""The kinds whose page transcription the student may correct by hand (a photographed page)."""


class NoTranscriptionError(SourceError):
    """A page with no transcription yet: there is nothing to correct by hand."""


def edit_page_transcription(
    vault: Vault,
    vault_relative_path: str,
    text: str,
    *,
    edited_at: datetime | None = None,
) -> Path:
    """Replace a page's transcription with the student's own correction (#473); return its path.

    `vault_relative_path` is a listed `notes` or `book` page (not a derived file, a sidecar or a
    removed page). The page must have a transcription already -- its `page-NNN.md`, or a sidecar
    `transcription` string --: the student corrects what the transcriber read, never races it.
    `text` (not blank once stripped) is written as `page-NNN.md`, ending in one newline, so
    everything that reads the page's transcription (the editor, the index, the Recursos viewer)
    reads the correction. Provenance is kept: the sidecar gains `transcription_edited: {at:
    <edited_at, now UTC by default>, by: student, previous_sha256: <sha256 of the replaced
    text>, original_sha256: <sha256 of the text before the first correction>}`, and the replaced
    text stays in git history (the caller commits what was pending first, then the correction).
    A sidecar `transcription` string is replaced by the correction too, so no reader keeps
    serving the old text. Both files are written atomically under the directory's lock and pass
    the secret guard.

    Raises:
        SourcePathError: when the path is not a page of `notes` or `book`.
        SourceNotFoundError: when no listed page is at that path (never stored, derived, removed).
        NoTranscriptionError: when the page has no transcription to correct.
        SourceFileError: when its sidecar is not a readable YAML mapping.
        ValueError: when `text` is blank.
        SecretRefused: when the text looks like it carries a key; nothing is written.
    """
    parts = _checked_parts(vault_relative_path)
    if parts[5] not in EDITABLE_TRANSCRIPTION_KINDS or _PAGE_NUMBER.match(parts[-1]) is None:
        raise SourcePathError(f"{vault_relative_path!r} is not a photographed page")
    body = text.strip("\n")
    if not body.strip():
        raise ValueError("a transcription cannot be blank")
    body = body.rstrip() + "\n"
    guard(body)
    directory = vault.path.joinpath(*parts[:-1])
    if not directory.is_dir() or directory.resolve() != vault.path.resolve().joinpath(*parts[:-1]):
        raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}")
    stem = parts[-1].split(".", 1)[0]
    target = directory / f"{stem}{TRANSCRIPTION_SUFFIX}"
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        names = {entry.name for _, entry in _source_entries(directory, parts[5])}
        if parts[-1] not in names:
            raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}")
        sidecar = _sidecar_of(directory / parts[-1])
        if sidecar.is_symlink() or not sidecar.is_file():
            raise SourceNotFoundError(f"there is no stored page at {vault_relative_path!r}")
        meta = _read_sidecar(sidecar) or {}
        if is_removed(meta):
            raise SourceNotFoundError(f"the source at {vault_relative_path!r} was removed")
        previous: str | None = None
        if target.is_file() and not target.is_symlink():
            previous = target.read_text(encoding="utf-8", errors="replace")
        if previous is None or not previous.strip():
            stored = meta.get("transcription")
            previous = stored if isinstance(stored, str) and stored.strip() else None
        if previous is None:
            raise NoTranscriptionError(f"the page at {vault_relative_path!r} is not transcribed")
        previous_sha256 = hashlib.sha256(previous.encode("utf-8")).hexdigest()
        earlier = meta.get(TRANSCRIPTION_EDITED_KEY)
        original = earlier.get("original_sha256") if isinstance(earlier, dict) else None
        meta[TRANSCRIPTION_EDITED_KEY] = {
            "at": edited_at or datetime.now(UTC),
            "by": "student",
            "previous_sha256": previous_sha256,
            "original_sha256": original if isinstance(original, str) else previous_sha256,
        }
        if isinstance(meta.get("transcription"), str):
            # A sidecar copy would go on serving the replaced text (`/meta`, Recursos).
            meta["transcription"] = body
        meta_text = dump_yaml(_META_ADAPTER.dump_python(meta, mode="json"))
        guard(meta_text)
        write_text_atomic(target, body)
        write_text_atomic(sidecar, meta_text)
    return target


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
_IMAGE_SOURCE = re.compile(r"^img-(\d{3,})\.(?:png|jpg|webp|svg)$")
_DERIVED_TRANSCRIPTION = "md"
_MEDIA_TYPES = mimetypes.MimeTypes()  # built-in table only: no host file makes it differ
_MEDIA_TYPE_OVERRIDES = {".md": "text/markdown", ".yaml": "application/yaml"}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"
# subjects/<subject>/topics/<topic>/sources/<kind>/<file>
_SOURCE_PATH_PARTS = 7


def list_sources(
    vault: Vault, subject_slug: str, topic_slug: str, *, include_removed: bool = False
) -> list[StoredSource]:
    """Every stored source of the topic, ordered by kind (`SOURCE_KINDS` order) then number.

    A source `remove_source` retired (its sidecar has `removed`) is left out unless
    `include_removed`.

    A source is the content `put_source` stored: `page-NNN.<ext>` under `notes`, `book` and `pdf`,
    `NNN-<slug>.md` under `web`, `img-NNN.<png|jpg|webp|svg>` under `images`. Sidecars (`.yaml`) and
    the derived files `sources` adds next to a page (`page-NNN.md`, `page-NNN.page.jpg`) are not
    sources of their own; a `page-NNN.md` counts as the source only when no other `page-NNN.<ext>`
    is there. Symlinks are never listed. A topic without `sources/` lists as empty.
    Nothing is written.

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
            meta = _read_sidecar(_sidecar_of(entry))
            if is_removed(meta) and not include_removed:
                continue
            listed.append(
                StoredSource(kind=kind, path=entry.relative_to(vault.path).as_posix(), meta=meta)
            )
    return listed


def removed_source_paths(vault: Vault, subject_slug: str, topic_slug: str) -> frozenset[str]:
    """The vault-relative paths of the topic's removed sources (`remove_source`); reads only.

    Raises what `list_sources` raises.
    """
    return frozenset(
        source.path
        for source in list_sources(vault, subject_slug, topic_slug, include_removed=True)
        if is_removed(source.meta)
    )


def remove_source(
    vault: Vault,
    vault_relative_path: str,
    *,
    removed_at: datetime | None = None,
    sha256: str | None = None,
    added_at: datetime | None = None,
) -> Path:
    """Retire one stored source (a soft delete, #451) and return its sidecar's path.

    `vault_relative_path` is a source as `list_sources` names it (not a derived file nor a
    sidecar). Its sidecar gains `removed: {at: <removed_at, now UTC by default>, by: student}`,
    written atomically under the directory's lock (a source without a sidecar gets one holding
    just that). Nothing is deleted: the content, its derived files (crop, transcriptions, PDF page
    files) and its git history stay, `read_source` still serves it, and `list_sources` leaves it
    out from now on. The caller commits. Other sources, triage `duplicate_of` references and the
    session events that mention it are left as they are. When `sha256` or `added_at` is given,
    the source is retired only while its sidecar records those values (checked under the lock):
    a caller retiring a source it stored itself never retires another that reused its path.

    Raises:
        SourcePathError: when the path is not a source path (see `read_source`).
        SourceNotFoundError: when no listed source is at that path -- none was ever stored there,
            it names a derived file or a sidecar, or it was removed already -- or the one there
            does not record the given `sha256`/`added_at`.
        SourceFileError: when its sidecar is not a readable YAML mapping.
        SecretRefused: never in practice (the sidecar was guarded when stored); nothing written.
    """
    parts = _checked_parts(vault_relative_path)
    directory = vault.path.joinpath(*parts[:-1])
    if not directory.is_dir() or directory.resolve() != vault.path.resolve().joinpath(*parts[:-1]):
        raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}")
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        names = {entry.name for _, entry in _source_entries(directory, parts[5])}
        if parts[-1] not in names:
            raise SourceNotFoundError(f"there is no source at {vault_relative_path!r}")
        sidecar = _sidecar_of(directory / parts[-1])
        if sidecar.is_symlink():
            raise SourcePathError(f"the sidecar of {vault_relative_path!r} is a symlink")
        meta = _read_sidecar(sidecar) or {}
        if is_removed(meta):
            raise SourceNotFoundError(f"the source at {vault_relative_path!r} was removed already")
        if not _records_identity(meta, sha256, added_at):
            raise SourceNotFoundError(f"the source at {vault_relative_path!r} is another one")
        meta[REMOVED_KEY] = {"at": removed_at or datetime.now(UTC), "by": "student"}
        text = dump_yaml(_META_ADAPTER.dump_python(meta, mode="json"))
        guard(text)
        write_text_atomic(sidecar, text)
    return sidecar


def retire_orphan_sidecar(
    vault: Vault,
    vault_relative_path: str,
    *,
    removed_at: datetime | None = None,
    sha256: str | None = None,
    added_at: datetime | None = None,
) -> Path | None:
    """Retire the sidecar a source's content left behind (#502) and return its path.

    `vault_relative_path` names a source's content file (same path rules as `read_source`). When
    that file is gone but its `.yaml` sidecar is still there -- a sync-loop batch commit took the
    sidecar of a source whose content a later revert removed -- the sidecar gains the same
    `removed: {at, by: student}` mapping `remove_source` writes, atomically under the directory's
    lock. Nothing is deleted and the caller commits. `None`, with nothing written, when the
    content is there, when any other file sharing the sidecar is there (`img-001.png` next to a
    gone `img-001.jpg`: the sidecar is that file's), when there is no sidecar (or it is a
    symlink), when it is marked removed already, or when `sha256`/`added_at` is given and the
    sidecar does not record it (it describes another source).

    Raises:
        SourcePathError: when the path is not a source path (see `read_source`).
        SourceFileError: when the sidecar is not a readable YAML mapping.
    """
    parts = _checked_parts(vault_relative_path)
    directory = vault.path.joinpath(*parts[:-1])
    if not directory.is_dir() or directory.resolve() != vault.path.resolve().joinpath(*parts[:-1]):
        return None
    content = directory / parts[-1]
    sidecar = _sidecar_of(content)
    if sidecar == content:
        return None
    with directory_lock(vault.path, directory).hold(SOURCE_LOCK_TIMEOUT_SECONDS):
        if content.exists() or content.is_symlink() or sidecar.is_symlink():
            return None
        if any(entry != sidecar and _sidecar_of(entry) == sidecar for entry in directory.iterdir()):
            return None
        meta = _read_sidecar(sidecar)
        if meta is None or is_removed(meta) or not _records_identity(meta, sha256, added_at):
            return None
        meta[REMOVED_KEY] = {"at": removed_at or datetime.now(UTC), "by": "student"}
        text = dump_yaml(_META_ADAPTER.dump_python(meta, mode="json"))
        guard(text)
        write_text_atomic(sidecar, text)
    return sidecar


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
        pattern = _IMAGE_SOURCE if kind == IMAGES_KIND else _WEB_SOURCE
        return [
            (int(match.group(1)), entry)
            for entry in entries
            if (match := pattern.match(entry.name)) is not None
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


def _records_identity(
    meta: Mapping[str, Any], sha256: str | None, added_at: datetime | None
) -> bool:
    """Whether the sidecar `meta` records `sha256` and `added_at`, each only when given."""
    if sha256 is not None and meta.get("sha256") != sha256:
        return False
    if added_at is None:
        return True
    try:
        return _INSTANT.validate_python(meta.get("added_at")) == added_at
    except ValidationError:
        return False


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
