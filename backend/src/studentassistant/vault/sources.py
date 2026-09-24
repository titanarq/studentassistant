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
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import TypeAdapter

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import dump_yaml, write_bytes_atomic, write_text_atomic
from studentassistant.vault.secrets import guard
from studentassistant.vault.slugs import slugify
from studentassistant.vault.topics import get_topic, topic_directory
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
_META_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])


class SourceError(VaultError):
    """A source this backend refuses to store; the message says why."""


class UnknownSourceKindError(SourceError):
    """The `kind` is not one of `notes`, `book`, `pdf`, `web`."""


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
) -> Path:
    """Store one source of a topic with its `.yaml` metadata sidecar and return the content's path.

    For `notes`, `book` and `pdf`, `name` is the file the content came as, and only its extension
    is kept (`foto.JPG` -> `page-004.jpg`). For `web`, `name` is the page's title and becomes the
    slug of `NNN-<slug>.md`. `content` is written as it is: bytes untouched, text as UTF-8. `meta`
    is dumped with the same deterministic YAML as every vault file; datetimes become ISO 8601.

    Raises:
        UnknownSourceKindError: when `kind` is not a source kind; nothing is written.
        SourceError: when a paged source's `name` has no usable extension.
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
    if isinstance(content, bytes):
        write_bytes_atomic(content_path, content)
    else:
        write_text_atomic(content_path, content)
    try:
        write_text_atomic(sidecar_path, sidecar_text)
    except BaseException:
        content_path.unlink(missing_ok=True)
        raise
    return content_path


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
