"""`vault_stats`: bytes by category, per subject and topic, the largest files and git's store."""

from __future__ import annotations

import subprocess
from pathlib import Path

from studentassistant.vault import Vault
from studentassistant.vault.stats import CATEGORIES, categorize, git_store_size, vault_stats

TOPIC = "subjects/mates/topics/derivadas"
OTHER_TOPIC = "subjects/mates/topics/integrales"

# Every path gets `size` bytes; the vault's own `vault.yaml` and `.gitattributes` add to "other".
FILES: dict[str, int] = {
    f"{TOPIC}/sources/notes/page-001.jpg": 5000,
    f"{TOPIC}/sources/notes/page-001.page.JPG": 3000,
    f"{TOPIC}/sources/notes/page-001.md": 200,
    f"{TOPIC}/sources/notes/page-001.yaml": 100,
    f"{TOPIC}/sources/pdf/page-001.pdf": 7000,
    f"{TOPIC}/sources/web/001-algo.md": 300,
    f"{TOPIC}/sessions/20260101-100000/events.jsonl": 900,
    f"{TOPIC}/sessions/20260101-100000/transcript.jsonl": 400,
    f"{TOPIC}/conversations/editor.jsonl": 1100,
    f"{TOPIC}/notes/apuntes.md": 600,
    f"{TOPIC}/generated/quiz.yaml": 250,
    f"{TOPIC}/study/quiz-results.jsonl": 50,
    f"{TOPIC}/topic.yaml": 10,
    f"{TOPIC}/state/digest.md": 40,
    f"{TOPIC}/ledger.jsonl": 20,
    f"{OTHER_TOPIC}/sources/book/page-001.png": 2000,
    "subjects/mates/subject.yaml": 30,
    "subjects/fisica/topics/ondas/notes/apuntes.md": 80,
}


def fill(vault: Vault) -> int:
    """Write `FILES` into the vault; return what the vault's own two files already weigh."""
    base = sum((vault.path / name).stat().st_size for name in ("vault.yaml", ".gitattributes"))
    for relative, size in FILES.items():
        path = vault.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return base


def by_category(vault: Vault) -> dict[str, int]:
    return {size.category: size.bytes for size in vault_stats(vault).categories}


def test_every_file_lands_in_its_category(tmp_vault: Vault) -> None:
    base = fill(tmp_vault)

    stats = vault_stats(tmp_vault)

    assert [size.category for size in stats.categories] == list(CATEGORIES)
    assert by_category(tmp_vault) == {
        "source_images": 5000 + 3000 + 2000,
        "pdfs": 7000,
        "other_sources": 200 + 100 + 300,
        "sessions": 900 + 400,
        "conversations": 1100,
        "notes": 600 + 80,
        "generated": 250,
        "study": 50,
        "other": 10 + 40 + 20 + 30 + base,
    }
    assert stats.working_tree_bytes == sum(FILES.values()) + base
    assert stats.working_tree_files == len(FILES) + 2
    assert {size.category: size.files for size in stats.categories}["source_images"] == 3


def test_git_directory_is_not_part_of_the_working_tree(tmp_vault: Vault) -> None:
    base = fill(tmp_vault)
    subprocess.run(["git", "add", "-A"], cwd=tmp_vault.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.invalid", "commit", "-qm", "x"],
        cwd=tmp_vault.path,
        check=True,
        capture_output=True,
    )

    stats = vault_stats(tmp_vault)

    assert stats.working_tree_bytes == sum(FILES.values()) + base
    assert stats.git is not None and stats.git.loose_objects > 0 and stats.git.loose_bytes > 0
    assert stats.total_bytes == stats.working_tree_bytes + stats.git.bytes


def test_packs_and_loose_objects_are_read_from_the_filesystem(tmp_vault: Vault) -> None:
    objects = tmp_vault.path / ".git" / "objects"
    for child in objects.rglob("*"):
        if child.is_file():
            child.unlink()
    (objects / "ab").mkdir(exist_ok=True)
    (objects / "ab" / "cdef").write_bytes(b"l" * 123)
    (objects / "pack").mkdir(exist_ok=True)
    (objects / "pack" / "pack-1.pack").write_bytes(b"p" * 1000)
    (objects / "pack" / "pack-1.idx").write_bytes(b"i" * 50)
    (objects / "info").mkdir(exist_ok=True)
    (objects / "info" / "packs").write_bytes(b"n" * 7)

    store = git_store_size(tmp_vault)

    assert store is not None
    assert (store.pack_bytes, store.packs, store.loose_bytes, store.loose_objects) == (
        1050,
        1,
        123,
        1,
    )


def test_totals_per_subject_and_topic_largest_first(tmp_vault: Vault) -> None:
    fill(tmp_vault)

    stats = vault_stats(tmp_vault)

    derivadas = sum(size for path, size in FILES.items() if path.startswith(TOPIC + "/"))
    assert [(subject.slug, subject.bytes) for subject in stats.subjects] == [
        ("mates", derivadas + 2000 + 30),
        ("fisica", 80),
    ]
    mates = stats.subjects[0]
    assert [(topic.slug, topic.bytes, topic.files) for topic in mates.topics] == [
        ("derivadas", derivadas, 15),
        ("integrales", 2000, 1),
    ]


def test_largest_files_are_the_top_n(tmp_vault: Vault) -> None:
    fill(tmp_vault)

    largest = vault_stats(tmp_vault, top=3).largest_files

    assert [(file.path, file.bytes, file.category) for file in largest] == [
        (f"{TOPIC}/sources/pdf/page-001.pdf", 7000, "pdfs"),
        (f"{TOPIC}/sources/notes/page-001.jpg", 5000, "source_images"),
        (f"{TOPIC}/sources/notes/page-001.page.JPG", 3000, "source_images"),
    ]
    assert vault_stats(tmp_vault, top=0).largest_files == []


def test_largest_categories_leave_out_empty_ones(tmp_vault: Vault) -> None:
    fill(tmp_vault)

    ranked = vault_stats(tmp_vault).largest_categories(3)

    assert [size.category for size in ranked] == ["source_images", "pdfs", "sessions"]


def test_links_are_not_followed_and_nothing_is_written(tmp_vault: Vault, tmp_path: Path) -> None:
    outside = tmp_path / "big.bin"
    outside.write_bytes(b"b" * 50_000)
    (tmp_vault.path / "link.bin").symlink_to(outside)
    before = sorted(p.relative_to(tmp_vault.path) for p in tmp_vault.path.rglob("*"))

    stats = vault_stats(tmp_vault)

    assert stats.working_tree_bytes < 50_000
    assert sorted(p.relative_to(tmp_vault.path) for p in tmp_vault.path.rglob("*")) == before


def test_categorize_only_counts_a_topic_area_inside_a_topic() -> None:
    assert categorize(("subjects", "s", "topics", "t", "sources", "x.pdf")) == "pdfs"
    assert categorize(("subjects", "s", "topics", "t", "notes", "apuntes.md")) == "notes"
    assert categorize(("notes", "apuntes.md")) == "other"
    assert categorize(("subjects", "s", "subject.yaml")) == "other"
    assert categorize(("subjects", "s", "topics", "t", "topic.yaml")) == "other"


def test_json_dump_carries_the_totals(tmp_vault: Vault) -> None:
    fill(tmp_vault)

    dumped = vault_stats(tmp_vault).model_dump(mode="json")

    assert dumped["total_bytes"] == dumped["working_tree_bytes"] + dumped["git_bytes"]
    assert "bytes" in dumped["git"]


# -- several students, one vault (#547) ----------------------------------------------------------

# Both users have a `mates/derivadas` on purpose: the same two slugs in two folders are two
# subjects, and one total for them would be nobody's.
USER_FILES: dict[str, int] = {
    "users/ana/profile.json": 120,
    "users/ana/photo.jpg": 4000,
    "users/ana/subjects/mates/subject.yaml": 30,
    "users/ana/subjects/mates/topics/derivadas/sources/notes/page-001.jpg": 9000,
    "users/ana/subjects/mates/topics/derivadas/notes/apuntes.md": 700,
    "users/bia/profile.json": 110,
    "users/bia/subjects/mates/topics/derivadas/sources/pdf/tema.pdf": 6000,
    "users/bia/subjects/mates/topics/integrales/notes/apuntes.md": 500,
}
ANA_BYTES = sum(size for path, size in USER_FILES.items() if path.startswith("users/ana/"))
BIA_BYTES = sum(size for path, size in USER_FILES.items() if path.startswith("users/bia/"))


def write_users(vault: Vault) -> None:
    """Give the vault two users and their content, as a migrated vault holds it."""
    for relative, size in USER_FILES.items():
        path = vault.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def own_bytes(vault: Vault) -> int:
    """What the vault's own two files weigh: every root total here carries them as `other`."""
    return sum((vault.root / name).stat().st_size for name in ("vault.yaml", ".gitattributes"))


def test_categorize_reads_a_users_path_as_the_same_path_inside_their_folder() -> None:
    user = ("users", "ana")
    assert categorize((*user, "subjects", "s", "topics", "t", "sources", "x.pdf")) == "pdfs"
    assert (
        categorize((*user, "subjects", "s", "topics", "t", "sources", "p.JPG")) == "source_images"
    )
    assert categorize((*user, "subjects", "s", "topics", "t", "sources", "p.md")) == "other_sources"
    assert categorize((*user, "subjects", "s", "topics", "t", "notes", "apuntes.md")) == "notes"
    assert categorize((*user, "subjects", "s", "topics", "t", "generated", "q.yaml")) == "generated"
    assert categorize((*user, "subjects", "s", "topics", "t", "topic.yaml")) == "other"
    assert categorize((*user, "subjects", "s", "subject.yaml")) == "other"
    assert categorize((*user, "profile.json")) == "other"
    assert categorize((*user, "photo.jpg")) == "other"


def test_a_root_handle_reports_each_user_with_their_own_subjects(tmp_vault: Vault) -> None:
    write_users(tmp_vault)

    stats = vault_stats(tmp_vault)

    assert [(user.id, user.bytes, user.files) for user in stats.users] == [
        ("ana", ANA_BYTES, 5),
        ("bia", BIA_BYTES, 3),
    ]
    ana, bia = stats.users
    assert [(subject.slug, subject.bytes) for subject in ana.subjects] == [
        ("mates", 9000 + 700 + 30)
    ]
    assert [(topic.slug, topic.bytes, topic.files) for topic in ana.subjects[0].topics] == [
        ("derivadas", 9700, 2)
    ]
    assert [(subject.slug, subject.bytes) for subject in bia.subjects] == [("mates", 6500)]
    assert [(topic.slug, topic.bytes) for topic in bia.subjects[0].topics] == [
        ("derivadas", 6000),
        ("integrales", 500),
    ]
    # The root has no subject of its own here, and the categories are the whole vault's.
    assert stats.subjects == []
    assert by_category(tmp_vault)["source_images"] == 9000
    assert by_category(tmp_vault)["pdfs"] == 6000
    assert by_category(tmp_vault)["notes"] == 700 + 500
    assert by_category(tmp_vault)["other"] == 120 + 4000 + 30 + 110 + own_bytes(tmp_vault)
    assert stats.working_tree_bytes == sum(USER_FILES.values()) + own_bytes(tmp_vault)
    assert stats.working_tree_files == len(USER_FILES) + 2
    assert [file.path for file in vault_stats(tmp_vault, top=1).largest_files] == [
        "users/ana/subjects/mates/topics/derivadas/sources/notes/page-001.jpg"
    ]


def test_a_user_handle_measures_their_folder_and_the_repositorys_store(tmp_vault: Vault) -> None:
    write_users(tmp_vault)
    ana = tmp_vault.for_user("ana")
    objects = tmp_vault.root / ".git" / "objects" / "ab"
    objects.mkdir(exist_ok=True)
    (objects / "cdef").write_bytes(b"l" * 123)

    stats = vault_stats(ana)

    assert stats.users == []  # the walk starts inside one user's folder: there is nobody else
    assert [(subject.slug, subject.bytes) for subject in stats.subjects] == [("mates", 9730)]
    assert (stats.working_tree_bytes, stats.working_tree_files) == (ANA_BYTES, 5)
    assert [file.path for file in vault_stats(ana, top=1).largest_files] == [
        "subjects/mates/topics/derivadas/sources/notes/page-001.jpg"
    ]
    # Git's store is the repository's: a user's handle reads it at the root, not under their folder.
    assert stats.git is not None and stats.git.loose_bytes == 123
    assert stats.git.bytes == git_store_size(tmp_vault).bytes
