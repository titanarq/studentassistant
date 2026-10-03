"""How big the vault is and what makes it big (issue #284). Pure reads: nothing here writes.

`vault_stats(vault)` walks the working tree (everything under the vault root except `.git`) and
sorts every regular file into a category by where the layout puts it (`docs/modules/vault.md`):
the images, PDFs and other files under a topic's `sources/`, its `sessions/`, `conversations/`,
`notes/`, `generated/` and `study/`, and `other` for the rest (`vault.yaml`, `topic.yaml`,
`state/`, `review/`, the ledgers...). It also sums each subject and topic, keeps the largest
files, and reads the size of git's object store (packs plus loose objects) straight from the
filesystem, so no git command runs and nothing in the repository changes.

A vault holds one folder per user (`users/<user-id>/`, epic #544) and the report says whose bytes
they are: on a root handle, `users` gives each user's own subjects and topics, because two students
may well study a subject of the same name and one number for both would be nobody's. What is the
repository's rather than one student's is measured there too -- git's object store is read at
`vault.root`, so a user's handle reports the history its own content is part of.

The numbers are what the open question "images in plain git or Git LFS" (VISION §10) is decided
with, and what `studentassistant doctor` compares against `[vault] size_warning_mb`.
"""

from __future__ import annotations

import heapq
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, computed_field

from studentassistant.vault.subjects import SUBJECTS_DIRNAME
from studentassistant.vault.topics import TOPICS_DIRNAME
from studentassistant.vault.vault import USERS_DIRNAME, Vault

Category = Literal[
    "source_images",
    "pdfs",
    "other_sources",
    "sessions",
    "conversations",
    "notes",
    "generated",
    "study",
    "other",
]

CATEGORIES: tuple[Category, ...] = (
    "source_images",
    "pdfs",
    "other_sources",
    "sessions",
    "conversations",
    "notes",
    "generated",
    "study",
    "other",
)

# What the student reads for each category (the CLI table and the doctor warning).
CATEGORY_LABELS: dict[Category, str] = {
    "source_images": "imágenes de fuentes",
    "pdfs": "PDF",
    "other_sources": "otras fuentes",
    "sessions": "sesiones (registros)",
    "conversations": "conversaciones",
    "notes": "apuntes",
    "generated": "material generado",
    "study": "estudio",
    "other": "otros",
}

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp"})
DEFAULT_TOP_FILES = 10

_TOPIC_DIRECTORIES: dict[str, Category] = {
    "sessions": "sessions",
    "conversations": "conversations",
    "notes": "notes",
    "generated": "generated",
    "study": "study",
}
_LOOSE_OBJECT_DIRECTORY = re.compile(r"[0-9a-f]{2}")


class CategorySize(BaseModel):
    category: Category
    label: str
    bytes: int
    files: int


class TopicSize(BaseModel):
    slug: str
    bytes: int
    files: int


class SubjectSize(BaseModel):
    """A subject's whole directory (its `subject.yaml` included) and each of its topics."""

    slug: str
    bytes: int
    files: int
    topics: list[TopicSize]


class UserSize(BaseModel):
    """One user's folder: their profile and photo, and each of their subjects and topics."""

    id: str
    bytes: int
    files: int
    subjects: list[SubjectSize]


class FileSize(BaseModel):
    path: str  # relative to the handle's path, `/`-separated: `users/<id>/...` on a root handle
    bytes: int
    category: Category


class GitStoreSize(BaseModel):
    """`.git/objects`: the packs (`objects/pack/*`) and the loose objects (`objects/xx/*`)."""

    pack_bytes: int
    packs: int
    loose_bytes: int
    loose_objects: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bytes(self) -> int:
        return self.pack_bytes + self.loose_bytes


class VaultStats(BaseModel):
    working_tree_bytes: int
    working_tree_files: int
    categories: list[CategorySize]  # every category, in `CATEGORIES` order
    subjects: list[SubjectSize]  # largest first; the ones directly under the handle's path
    users: list[UserSize] = []  # largest first; a root handle's users, empty on a user's handle
    largest_files: list[FileSize]  # largest first
    git: GitStoreSize | None  # `None` when the vault has no `.git` directory

    @computed_field  # type: ignore[prop-decorator]
    @property
    def git_bytes(self) -> int:
        return self.git.bytes if self.git is not None else 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_bytes(self) -> int:
        """What the vault occupies on disk: the working tree plus git's object store."""
        return self.working_tree_bytes + self.git_bytes

    def largest_categories(self, count: int = 3) -> list[CategorySize]:
        """The `count` categories holding the most bytes, empty ones left out."""
        ranked = sorted(
            (size for size in self.categories if size.bytes > 0),
            key=lambda size: (-size.bytes, CATEGORIES.index(size.category)),
        )
        return ranked[:count]


def categorize(relative: tuple[str, ...]) -> Category:
    """The category of the vault-relative path given as its parts.

    A user's path categorizes as the same path inside their folder -- `users/ana/subjects/...` is a
    topic's exactly as `subjects/...` is, because a topic's layout is the same under whichever user
    it lives -- and what no topic area covers is `other`, their `profile.json` and `photo.jpg`
    included.
    """
    return _categorize(_split_user(relative)[1])


def _categorize(relative: tuple[str, ...]) -> Category:
    """The category of a path inside one user's folder (or the root's own content)."""
    if len(relative) >= 6 and relative[0] == SUBJECTS_DIRNAME and relative[2] == TOPICS_DIRNAME:
        area = relative[4]
        if area == "sources":
            suffix = Path(relative[-1]).suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                return "source_images"
            if suffix == ".pdf":
                return "pdfs"
            return "other_sources"
        if area in _TOPIC_DIRECTORIES:
            return _TOPIC_DIRECTORIES[area]
    return "other"


def _split_user(parts: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """The user a path belongs to and the path inside their folder: `users/<id>/x` -> `(<id>, x)`.

    `None` for a path of the repository's own content, and for every path a user's handle walks,
    which starts inside that user's folder already and so never carries the `users/` prefix.
    """
    if len(parts) >= 2 and parts[0] == USERS_DIRNAME:
        return parts[1], parts[2:]
    return None, parts


def _regular_files(root: Path, skip_git: bool) -> list[tuple[tuple[str, ...], int]]:
    """Every regular file under `root` as (parts relative to `root`, size); links not followed."""
    found: list[tuple[tuple[str, ...], int]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(directory)
        if skip_git and here == root and ".git" in dirnames:
            dirnames.remove(".git")
        relative_dir = here.relative_to(root).parts
        for name in filenames:
            try:
                info = (here / name).lstat()
            except OSError:
                continue  # vanished while walking
            if stat.S_ISREG(info.st_mode):
                found.append(((*relative_dir, name), info.st_size))
    return found


def git_store_size(vault: Vault) -> GitStoreSize | None:
    """The size of `.git/objects`, read from the filesystem (no git command runs).

    The object store is the repository's, so it is read at `vault.root`: a user's handle reports
    the history its own content is committed into, and there is no `.git/` inside their folder.
    """
    objects = vault.root / ".git" / "objects"
    if not objects.is_dir():
        return None
    pack_bytes = packs = loose_bytes = loose_objects = 0
    for parts, size in _regular_files(objects, skip_git=False):
        if parts[0] == "pack":
            pack_bytes += size
            packs += parts[-1].endswith(".pack")
        elif len(parts) == 2 and _LOOSE_OBJECT_DIRECTORY.fullmatch(parts[0]):
            loose_bytes += size
            loose_objects += 1
    return GitStoreSize(
        pack_bytes=pack_bytes, packs=packs, loose_bytes=loose_bytes, loose_objects=loose_objects
    )


class _Totals:
    """Bytes and file counts of one walk, per subject and per topic: what a report is built from.

    One instance totals the whole of what a handle walks and one each user's folder of it, so a
    root handle gives every user their own subjects without walking the tree twice.
    """

    def __init__(self) -> None:
        self.bytes = 0
        self.files = 0
        self._subjects: dict[str, list[int]] = {}
        self._topics: dict[tuple[str, str], list[int]] = {}

    def add(self, parts: tuple[str, ...], size: int) -> None:
        """Count one file, `parts` being its path relative to what this instance totals up."""
        self.bytes += size
        self.files += 1
        if len(parts) >= 3 and parts[0] == SUBJECTS_DIRNAME:
            subject = self._subjects.setdefault(parts[1], [0, 0])
            subject[0] += size
            subject[1] += 1
            if len(parts) >= 5 and parts[2] == TOPICS_DIRNAME:
                topic = self._topics.setdefault((parts[1], parts[3]), [0, 0])
                topic[0] += size
                topic[1] += 1

    def subjects(self) -> list[SubjectSize]:
        """Each subject with each of its topics, the largest first and ties by slug."""
        return sorted(
            (
                SubjectSize(
                    slug=slug,
                    bytes=size,
                    files=count,
                    topics=sorted(
                        (
                            TopicSize(slug=topic_slug, bytes=topic_size, files=topic_count)
                            for (subject_slug, topic_slug), (
                                topic_size,
                                topic_count,
                            ) in self._topics.items()
                            if subject_slug == slug
                        ),
                        key=lambda topic: (-topic.bytes, topic.slug),
                    ),
                )
                for slug, (size, count) in self._subjects.items()
            ),
            key=lambda subject: (-subject.bytes, subject.slug),
        )


def vault_stats(vault: Vault, top: int = DEFAULT_TOP_FILES) -> VaultStats:
    """Measure the vault: categories, subjects and topics per user, the largest files, git's store.

    `users` is what a root handle adds for a vault that has them: one entry per user folder with
    that user's own subjects and topics, `subjects` then holding only what sits directly under the
    root -- nothing in that layout, because a student's content is inside their folder and two of
    them may well have a subject of the same slug. On a user's handle the walk starts inside their
    folder, so `subjects` is that student's and `users` is empty.
    """
    by_category: dict[Category, list[int]] = {category: [0, 0] for category in CATEGORIES}
    whole = _Totals()
    per_user: dict[str, _Totals] = {}
    files: list[FileSize] = []
    entries = _regular_files(vault.path, skip_git=True)
    for parts, size in entries:
        category = categorize(parts)
        by_category[category][0] += size
        by_category[category][1] += 1
        whole.add(parts, size)
        user_id, inside = _split_user(parts)
        if user_id is not None:
            per_user.setdefault(user_id, _Totals()).add(inside, size)
        files.append(FileSize(path="/".join(parts), bytes=size, category=category))
    largest = heapq.nsmallest(max(top, 0), files, key=lambda file: (-file.bytes, file.path))
    return VaultStats(
        working_tree_bytes=whole.bytes,
        working_tree_files=len(entries),
        categories=[
            CategorySize(
                category=category,
                label=CATEGORY_LABELS[category],
                bytes=by_category[category][0],
                files=by_category[category][1],
            )
            for category in CATEGORIES
        ],
        subjects=whole.subjects(),
        users=sorted(
            (
                UserSize(
                    id=user_id,
                    bytes=totals.bytes,
                    files=totals.files,
                    subjects=totals.subjects(),
                )
                for user_id, totals in per_user.items()
            ),
            key=lambda user: (-user.bytes, user.id),
        ),
        largest_files=largest,
        git=git_store_size(vault),
    )
