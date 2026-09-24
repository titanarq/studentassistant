"""Topics: the second level of the vault layout, one directory per topic inside its subject.

A topic is the unit of work the whole product revolves around: the pages a session captured, the
transcript of what the student said about them, the master notes the tutor-editor builds and
everything generated from them are all written under
`subjects/<subject-slug>/topics/<topic-slug>/`. What this module writes is only the first file of
that tree (`topic.yaml`: the title the student gave it, how faithful the editor must be, when it
was created and which sessions fed it), and what it decides is the slug that becomes the name of
the directory the rest of it lands in.

Two rules follow, the same two `subjects.py` states for a subject. The slug comes from the Spanish
title and is unique among the topics of its own subject only: "Derivadas" under Matemáticas and
"Derivadas" under Física are two topics with two directories, because the path is the identity and
neither one's content may be written over the other's. And a title whose slug another topic of the
same subject already has gets a numeric suffix, so re-reading the same chapter aloud in a second
session cannot silently merge two topics of the vault, which is the source of truth for both
(ADR-0002).

Nothing here writes under a directory that is not a subject. Every entry point reads the subject
first and lets its error through, so an unknown subject is a `SubjectNotFoundError` and a subject
whose `subject.yaml` this backend cannot read is a `SubjectFileError`: a topic written into a
directory no listing reaches is content the student cannot open any more. Reading a topic is as
strict as reading a subject -- a directory under `topics/` whose `topic.yaml` is missing or
unreadable is refused with an error naming the file, rather than left out of the listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from studentassistant.vault.files import read_yaml, write_yaml_atomic
from studentassistant.vault.models import Topic
from studentassistant.vault.slugs import slugify, unique_slug
from studentassistant.vault.subjects import get_subject, subject_directory
from studentassistant.vault.vault import Vault, VaultError

TOPICS_DIRNAME = "topics"
TOPIC_FILE_NAME = "topic.yaml"


class TopicError(VaultError):
    """A topic this backend cannot create, find or read; the message says which and why."""


class TopicNotFoundError(TopicError):
    """That subject of the vault has no topic with that slug."""


class TopicFileError(TopicError):
    """`topic.yaml` is missing, or holds something this backend cannot read as a `Topic`."""


@dataclass(frozen=True)
class StoredTopic:
    """A topic as the vault holds it: the slug naming its directory and its `topic.yaml`."""

    slug: str
    topic: Topic


def topics_directory(vault: Vault, subject_slug: str) -> Path:
    """Where the topics of `subject_slug` live, whether or not there is one yet."""
    return subject_directory(vault, subject_slug) / TOPICS_DIRNAME


def topic_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The directory the topic called `topic_slug` lives in, whether or not it exists yet."""
    return topics_directory(vault, subject_slug) / topic_slug


def create_topic(vault: Vault, subject_slug: str, title: str) -> StoredTopic:
    """Create `subjects/<subject-slug>/topics/<slug>/topic.yaml` for the topic called `title`.

    The slug is `slugify(title)`, suffixed with the first free number when another topic of the same
    subject already has it, so the new topic gets a directory of its own. The topic is born with no
    session: `sessions` stays empty until the session store records one against it, and
    `fidelity_mode` is the default `estricto`, the mode in which the tutor-editor keeps to what the
    pages and the student's own words say.

    Raises:
        SubjectNotFoundError: when the vault has no subject called `subject_slug`.
        SubjectFileError: when that subject's `subject.yaml` is missing or unreadable.
        ValueError: when `title` holds no letter and no digit, so there is no slug to name the
            directory after; nothing is written.
        FileExistsError: when the directory of the slug that was picked is there already, which is
            what creating the same topic twice at once looks like; nothing is overwritten.
        OSError: when the directory cannot be made or the file cannot be written.
    """
    _require_subject(vault, subject_slug)
    slug = unique_slug(slugify(title), _taken_slugs(vault, subject_slug))
    directory = topic_directory(vault, subject_slug, slug)
    directory.mkdir(parents=True)
    topic = Topic(title=title, created_at=datetime.now(UTC))
    write_yaml_atomic(directory / TOPIC_FILE_NAME, topic)
    return StoredTopic(slug=slug, topic=topic)


def list_topics(vault: Vault, subject_slug: str) -> list[StoredTopic]:
    """Every topic of one subject, sorted by slug, so a listing never depends on the file system.

    A subject that has no topic yet has no `topics/` directory either, and lists as empty.

    Raises:
        SubjectNotFoundError: when the vault has no subject called `subject_slug`.
        SubjectFileError: when that subject's `subject.yaml` is missing or unreadable.
        TopicFileError: when a directory under its `topics/` has no `topic.yaml` this backend can
            read as a `Topic`; the topic stays in the listing as an error instead of vanishing.
    """
    _require_subject(vault, subject_slug)
    slugs = sorted(_taken_slugs(vault, subject_slug))
    return [_read_topic(vault, subject_slug, slug) for slug in slugs]


def get_topic(vault: Vault, subject_slug: str, topic_slug: str) -> StoredTopic:
    """Read the topic called `topic_slug`, which is the whole of its `topic.yaml`.

    Raises:
        SubjectNotFoundError: when the vault has no subject called `subject_slug`.
        SubjectFileError: when that subject's `subject.yaml` is missing or unreadable.
        TopicNotFoundError: when the subject has no such topic directory.
        TopicFileError: when the directory is there but its `topic.yaml` is missing, unreadable,
            not YAML, or holds fields that are not a `Topic`.
    """
    _require_subject(vault, subject_slug)
    return _read_topic(vault, subject_slug, topic_slug)


def _require_subject(vault: Vault, subject_slug: str) -> None:
    """Refuse a topic of a directory that is not a subject this backend can read.

    Raises:
        SubjectNotFoundError: when the vault has no subject called `subject_slug`.
        SubjectFileError: when that subject's `subject.yaml` is missing or unreadable.
    """
    get_subject(vault, subject_slug)


def _read_topic(vault: Vault, subject_slug: str, topic_slug: str) -> StoredTopic:
    """Read one topic's directory, telling apart a slug that is not there from a file that is wrong.

    Raises:
        TopicNotFoundError: when the subject has no such topic directory.
        TopicFileError: when the directory is there but its `topic.yaml` cannot be read as a
            `Topic`.
    """
    directory = topic_directory(vault, subject_slug, topic_slug)
    if not directory.is_dir():
        raise TopicNotFoundError(
            f"the subject {subject_slug!r} of the vault at {vault.path} has no topic"
            f" {topic_slug!r}: {directory} is not a directory"
        )
    return StoredTopic(slug=topic_slug, topic=_read_topic_file(directory / TOPIC_FILE_NAME))


def _taken_slugs(vault: Vault, subject_slug: str) -> list[str]:
    """The slugs already in use under one subject's `topics/`.

    Every directory counts, including one whose `topic.yaml` is missing: a half-written topic still
    holds the sources and the sessions a later write may add to it, and a new topic taking its slug
    would write over them.
    """
    root = topics_directory(vault, subject_slug)
    if not root.is_dir():
        return []
    return [entry.name for entry in root.iterdir() if entry.is_dir()]


def _read_topic_file(topic_path: Path) -> Topic:
    """Read `topic.yaml`, naming in the error which of the ways it can be wrong it is.

    Raises:
        TopicFileError: when the file is missing, unreadable, not YAML or not a `Topic`.
    """
    try:
        return read_yaml(topic_path, Topic)
    except FileNotFoundError as error:
        raise TopicFileError(
            f"{topic_path} is missing, so {topic_path.parent} does not hold a topic"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise TopicFileError(f"{topic_path} cannot be read: {error}") from error
    except YAMLError as error:
        raise TopicFileError(f"{topic_path} is not YAML this backend can parse: {error}") from error
    except ValidationError as error:
        raise TopicFileError(f"{topic_path} does not hold a topic: {error}") from error
