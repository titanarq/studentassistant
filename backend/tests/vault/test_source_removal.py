"""`remove_source`: the soft delete of one stored source (#451). Nothing is deleted; the source
leaves `list_sources` (and the index) but `read_source` still serves every file of it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from studentassistant.vault import (
    REMOVED_KEY,
    SourceNotFoundError,
    SourcePathError,
    Vault,
    create_subject,
    create_topic,
    is_removed,
    list_sources,
    put_page_transcription,
    put_pasted_image,
    put_source,
    read_source,
    remove_source,
    removed_source_paths,
)
from studentassistant.vault.index import VaultIndex

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(32))
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
WHEN = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Física").slug
    return subject, create_topic(tmp_vault, subject, "Cinemática").slug


def _rel(vault: Vault, path: Path) -> str:
    return path.relative_to(vault.path).as_posix()


def _stored(vault: Vault, topic: tuple[str, str]) -> dict[str, tuple[str, list[str]]]:
    """One source of each kind: `kind -> (source path, its derived files' paths)`."""
    s, t = topic
    notes = put_source(
        vault, s, t, "notes", "foto.jpg", JPEG, {"capture_id": "c1"}, derived={"page.jpg": JPEG}
    )
    put_page_transcription(vault, _rel(vault, notes), "# La velocidad\n")
    book = put_source(vault, s, t, "book", "libro.jpg", JPEG, {"capture_id": "c2"})
    pdf = put_source(
        vault,
        s,
        t,
        "pdf",
        "tema.pdf",
        b"%PDF-1.4 fake",
        {"original_name": "tema.pdf"},
        derived={"p001.txt": "aceleración media", "p001.jpg": JPEG},
    )
    web = put_source(vault, s, t, "web", "Movimiento", "# MRU\n", {"url": "https://x.invalid"})
    image = put_pasted_image(vault, s, t, PNG, "image/png", added_at=WHEN)
    derived_of = {
        "notes": [notes.with_name("page-001.page.jpg"), notes.with_name("page-001.md")],
        "book": [],
        "pdf": [pdf.with_name("page-001.p001.txt"), pdf.with_name("page-001.p001.jpg")],
        "web": [],
        "images": [],
    }
    return {
        kind: (_rel(vault, path), [_rel(vault, d) for d in derived_of[kind]])
        for kind, path in (
            ("notes", notes),
            ("book", book),
            ("pdf", pdf),
            ("web", web),
            ("images", image),
        )
    }


@pytest.mark.parametrize("kind", ["notes", "book", "pdf", "web", "images"])
def test_a_removed_source_leaves_the_listing_but_keeps_every_file(
    tmp_vault: Vault, topic: tuple[str, str], kind: str
) -> None:
    stored = _stored(tmp_vault, topic)
    path, derived = stored[kind]
    before = {p: read_source(tmp_vault, p).content for p in [path, *derived]}

    sidecar = remove_source(tmp_vault, path, removed_at=WHEN)

    listed = [source.path for source in list_sources(tmp_vault, *topic)]
    assert path not in listed
    assert sorted(listed) == sorted(p for k, (p, _) in stored.items() if k != kind)
    everything = list_sources(tmp_vault, *topic, include_removed=True)
    assert path in [source.path for source in everything]
    meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    assert meta[REMOVED_KEY] == {"at": "2026-09-27T10:00:00Z", "by": "student"}
    assert is_removed(read_source(tmp_vault, path).meta)
    for kept, content in before.items():  # content and derived files stay, still served
        assert read_source(tmp_vault, kept).content == content
    assert removed_source_paths(tmp_vault, *topic) == {path}


def test_other_sidecar_keys_are_kept(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    path, _ = _stored(tmp_vault, topic)["notes"]
    remove_source(tmp_vault, path)
    meta = read_source(tmp_vault, path).meta or {}
    assert meta["capture_id"] == "c1"
    assert set(meta[REMOVED_KEY]) == {"at", "by"}


def test_numbering_goes_on_past_a_removed_source(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    path, _ = _stored(tmp_vault, topic)["book"]
    remove_source(tmp_vault, path)
    again = put_source(tmp_vault, *topic, "book", "otra.jpg", JPEG, {})
    assert again.name == "page-002.jpg"


def test_removing_twice_or_a_file_that_is_no_source_is_not_found(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    stored = _stored(tmp_vault, topic)
    path, derived = stored["notes"]
    remove_source(tmp_vault, path)
    with pytest.raises(SourceNotFoundError):
        remove_source(tmp_vault, path)
    for not_a_source in [*derived, path.replace(".jpg", ".yaml"), path.replace("001", "099")]:
        with pytest.raises(SourceNotFoundError):
            remove_source(tmp_vault, not_a_source)
    s, t = topic
    with pytest.raises(SourceNotFoundError):  # a kind with no directory yet
        remove_source(tmp_vault, f"subjects/{s}/topics/{t}/sources/book/page-009.jpg")


@pytest.mark.parametrize(
    "bad",
    ["", "/etc/passwd", "subjects/fisica/topics/cinematica/sources/../../vault.yaml", "vault.yaml"],
)
def test_a_path_that_is_no_source_path_is_refused(
    tmp_vault: Vault, topic: tuple[str, str], bad: str
) -> None:
    with pytest.raises(SourcePathError):
        remove_source(tmp_vault, bad)


def test_the_index_forgets_a_removed_source_and_its_texts(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    stored = _stored(tmp_vault, topic)
    with VaultIndex.open(tmp_vault, tmp_path / "index.sqlite3") as index:
        assert index.search("velocidad") != []
        assert index.search("aceleracion") != []
        remove_source(tmp_vault, stored["notes"][0])
        remove_source(tmp_vault, stored["pdf"][0])
        index.update()
        assert index.search("velocidad") == []
        assert index.search("aceleracion") == []
        assert [s.path for s in index.sources()] == [
            stored["book"][0],
            stored["web"][0],
            stored["images"][0],
        ]
