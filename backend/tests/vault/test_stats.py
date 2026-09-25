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
