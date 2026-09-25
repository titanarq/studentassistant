"""How big the vault is and what makes it big (issue #284). Pure reads: nothing here writes.

`vault_stats(vault)` walks the working tree (everything under the vault root except `.git`) and
sorts every regular file into a category by where the layout puts it (`docs/modules/vault.md`):
the images, PDFs and other files under a topic's `sources/`, its `sessions/`, `conversations/`,
`notes/`, `generated/` and `study/`, and `other` for the rest (`vault.yaml`, `topic.yaml`,
`state/`, `review/`, the ledgers...). It also sums each subject and topic, keeps the largest
files, and reads the size of git's object store (packs plus loose objects) straight from the
filesystem, so no git command runs and nothing in the repository changes.

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

from studentassistant.vault.vault import Vault

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


class FileSize(BaseModel):
    path: str  # vault-relative, `/`-separated
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
    subjects: list[SubjectSize]  # largest first
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
    """The category of the vault-relative path given as its parts."""
    if len(relative) >= 6 and relative[0] == "subjects" and relative[2] == "topics":
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
    """The size of `.git/objects`, read from the filesystem (no git command runs)."""
    objects = vault.path / ".git" / "objects"
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


def vault_stats(vault: Vault, top: int = DEFAULT_TOP_FILES) -> VaultStats:
    """Measure the vault: categories, subjects and topics, the `top` largest files, git's store."""
    by_category: dict[Category, list[int]] = {category: [0, 0] for category in CATEGORIES}
    by_subject: dict[str, list[int]] = {}
    by_topic: dict[tuple[str, str], list[int]] = {}
    files: list[FileSize] = []
    total = 0
    entries = _regular_files(vault.path, skip_git=True)
    for parts, size in entries:
        category = categorize(parts)
        by_category[category][0] += size
        by_category[category][1] += 1
        total += size
        if len(parts) >= 3 and parts[0] == "subjects":
            subject = by_subject.setdefault(parts[1], [0, 0])
            subject[0] += size
            subject[1] += 1
            if len(parts) >= 5 and parts[2] == "topics":
                topic = by_topic.setdefault((parts[1], parts[3]), [0, 0])
                topic[0] += size
                topic[1] += 1
        files.append(FileSize(path="/".join(parts), bytes=size, category=category))
    subjects = [
        SubjectSize(
            slug=slug,
            bytes=size,
            files=count,
            topics=sorted(
                (
                    TopicSize(slug=topic_slug, bytes=topic_size, files=topic_count)
                    for (subject_slug, topic_slug), (topic_size, topic_count) in by_topic.items()
                    if subject_slug == slug
                ),
                key=lambda topic: (-topic.bytes, topic.slug),
            ),
        )
        for slug, (size, count) in by_subject.items()
    ]
    subjects.sort(key=lambda subject: (-subject.bytes, subject.slug))
    largest = heapq.nsmallest(max(top, 0), files, key=lambda file: (-file.bytes, file.path))
    return VaultStats(
        working_tree_bytes=total,
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
        subjects=subjects,
        largest_files=largest,
        git=git_store_size(vault),
    )
