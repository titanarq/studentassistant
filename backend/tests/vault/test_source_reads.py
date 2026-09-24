"""Reading sources back: listing order, sidecars, media types and every refused path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from studentassistant.vault import (
    SourceContent,
    SourceError,
    SourceFileError,
    SourceNotFoundError,
    SourcePathError,
    StoredSource,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    put_source,
    read_source,
    sources_directory,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(64))


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Química").slug
    return subject, create_topic(tmp_vault, subject, "Enlace químico").slug


def _rel(vault: Vault, path: Path) -> str:
    return path.relative_to(vault.path).as_posix()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_a_topic_without_sources_lists_as_empty(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    assert list_sources(tmp_vault, *topic) == []


def test_sources_are_listed_by_kind_then_number_without_sidecars_or_derived_files(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    web = put_source(tmp_vault, *topic, "web", "Enlace iónico", "# Enlace", {"url": "https://x"})
    pdf = put_source(tmp_vault, *topic, "pdf", "tema.pdf", b"%PDF-1.7", {"pages": 3})
    first = put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {"capture_id": "c1"})
    second = put_source(tmp_vault, *topic, "notes", "b.png", JPEG, {"capture_id": "c2"})
    notes = sources_directory(tmp_vault, *topic, "notes")
    (notes / "page-001.md").write_text("transcripción", encoding="utf-8")
    (notes / "page-001.page.jpg").write_bytes(JPEG)
    # Numbers sort as numbers, not as text: page-1000 comes after page-002.
    (notes / "page-1000.jpg").write_bytes(JPEG)

    listed = list_sources(tmp_vault, *topic)

    assert [(s.kind, s.path) for s in listed] == [
        ("notes", _rel(tmp_vault, first)),
        ("notes", _rel(tmp_vault, second)),
        ("notes", _rel(tmp_vault, notes / "page-1000.jpg")),
        ("pdf", _rel(tmp_vault, pdf)),
        ("web", _rel(tmp_vault, web)),
    ]
    assert listed[0] == StoredSource(
        kind="notes",
        path=f"subjects/{topic[0]}/topics/{topic[1]}/sources/notes/page-001.jpg",
        meta={"capture_id": "c1"},
    )
    assert listed[2].meta is None
    assert listed[3].meta == {"pages": 3}
    assert listed[4].meta == {"url": "https://x"}


def test_a_page_whose_only_content_is_markdown_is_listed(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    stored = put_source(tmp_vault, *topic, "book", "capitulo.md", "# Capítulo", {"book": "X"})
    assert [s.path for s in list_sources(tmp_vault, *topic)] == [_rel(tmp_vault, stored)]


def test_symlinks_are_not_listed(tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"outside")
    (sources_directory(tmp_vault, *topic, "notes") / "page-002.jpg").symlink_to(outside)
    assert [s.path.rsplit("/", 1)[1] for s in list_sources(tmp_vault, *topic)] == ["page-001.jpg"]


def test_an_unreadable_sidecar_is_a_source_file_error(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    (sources_directory(tmp_vault, *topic, "notes") / "page-001.yaml").write_text(
        "- not\n- a mapping\n", encoding="utf-8"
    )
    with pytest.raises(SourceFileError):
        list_sources(tmp_vault, *topic)


@pytest.mark.parametrize(
    ("subject", "topic_slug", "error"),
    [
        ("..", "enlace-quimico", SubjectNotFoundError),
        ("quimica", "..", TopicNotFoundError),
        ("quimica", "../../quimica/topics/enlace-quimico", TopicNotFoundError),
        ("quimica", "no-existe", TopicNotFoundError),
    ],
)
def test_listing_refuses_what_is_not_a_topic(
    tmp_vault: Vault,
    topic: tuple[str, str],
    subject: str,
    topic_slug: str,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        list_sources(tmp_vault, subject, topic_slug)


def test_read_source_returns_bytes_sidecar_and_media_type(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    page = put_source(tmp_vault, *topic, "notes", "a.JPG", JPEG, {"capture_id": "c1"})
    web = put_source(tmp_vault, *topic, "web", "Página", "# Hola", {"url": "https://x"})
    (page.parent / "page-001.md").write_text("texto", encoding="utf-8")
    unknown = put_source(tmp_vault, *topic, "pdf", "raro.qqq", b"???", {})
    before = _snapshot(tmp_vault.path)

    assert read_source(tmp_vault, _rel(tmp_vault, page)) == SourceContent(
        content=JPEG, meta={"capture_id": "c1"}, media_type="image/jpeg"
    )
    web_read = read_source(tmp_vault, _rel(tmp_vault, web))
    assert web_read.content == b"# Hola"
    assert web_read.media_type == "text/markdown"
    assert web_read.meta == {"url": "https://x"}
    derived = read_source(tmp_vault, _rel(tmp_vault, page.parent / "page-001.md"))
    assert derived.meta == {"capture_id": "c1"}
    sidecar = read_source(tmp_vault, _rel(tmp_vault, page.parent / "page-001.yaml"))
    assert sidecar.meta is None
    assert sidecar.media_type == "application/yaml"
    assert read_source(tmp_vault, _rel(tmp_vault, unknown)).media_type == (
        "application/octet-stream"
    )
    assert _snapshot(tmp_vault.path) == before


def test_every_listed_path_is_readable(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {"n": 1})
    put_source(tmp_vault, *topic, "web", "Web", "# W", {"n": 2})
    for source in list_sources(tmp_vault, *topic):
        assert read_source(tmp_vault, source.path).meta == source.meta


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../vault.yaml",
        "subjects/quimica/topics/enlace-quimico/sources/notes/../../topic.yaml",
        "subjects/quimica/topics/enlace-quimico/sources/notes/../../../../../vault.yaml",
        "subjects/../vault.yaml",
        "subjects/quimica/topics/enlace-quimico/sources/./notes/page-001.jpg",
        "subjects/quimica/topics/enlace-quimico/sources/notes//page-001.jpg",
        "subjects/quimica/topics/enlace-quimico/sources/notes/page-001.jpg/",
        "subjects/quimica/topics/enlace-quimico/sources/notes\\..\\..\\topic.yaml",
        "subjects/quimica/topics/enlace-quimico/sources/notes/page-001.jpg\x00.png",
        "subjects/quimica/topics/enlace-quimico/topic.yaml",
        "subjects/quimica/topics/enlace-quimico/sources/other/page-001.jpg",
        "subjects/quimica/topics/enlace-quimico/sources/notes/sub/page-001.jpg",
        "subjects/quimica/topics/enlace-quimico/notes/apuntes.md",
        "vault.yaml",
        ".git/config",
        "subjects/Quimica/topics/enlace-quimico/sources/notes/page-001.jpg",
        "subjects/%2e%2e/topics/enlace-quimico/sources/notes/page-001.jpg",
    ],
)
def test_read_source_refuses_paths_that_are_not_a_source(
    tmp_vault: Vault, topic: tuple[str, str], path: str
) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, path)


def test_read_source_refuses_absolute_paths(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    page = put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, str(page))
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, "/etc/passwd")


def test_encoded_looking_segments_are_taken_literally(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    base = f"subjects/{topic[0]}/topics/{topic[1]}/sources/notes"
    for name in ("%2e%2e", "%2e%2e%2fvault.yaml", "..%2f..%2ftopic.yaml", "page-001%2ejpg"):
        with pytest.raises(SourceNotFoundError):
            read_source(tmp_vault, f"{base}/{name}")


def test_read_source_refuses_a_missing_file(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    base = f"subjects/{topic[0]}/topics/{topic[1]}/sources/notes"
    with pytest.raises(SourceNotFoundError):
        read_source(tmp_vault, f"{base}/page-001.jpg")
    with pytest.raises(SourceNotFoundError):
        read_source(tmp_vault, f"subjects/{topic[0]}/topics/otro/sources/notes/page-001.jpg")


def test_read_source_refuses_a_symlink_pointing_out_of_the_vault(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not the student's")
    link = sources_directory(tmp_vault, *topic, "notes") / "page-002.jpg"
    link.symlink_to(outside)
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, _rel(tmp_vault, link))


def test_read_source_refuses_a_symlink_to_another_vault_file(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    link = sources_directory(tmp_vault, *topic, "notes") / "page-002.yaml"
    link.symlink_to(tmp_vault.path / "vault.yaml")
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, _rel(tmp_vault, link))


def test_read_source_refuses_a_symlinked_sources_directory(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page-001.jpg").write_bytes(b"outside")
    sources = sources_directory(tmp_vault, *topic, "notes").parent
    sources.mkdir()
    os.symlink(elsewhere, sources / "book")
    with pytest.raises(SourcePathError):
        read_source(tmp_vault, _rel(tmp_vault, sources / "book" / "page-001.jpg"))


def test_a_symlinked_sidecar_is_not_followed(
    tmp_vault: Vault, topic: tuple[str, str], tmp_path: Path
) -> None:
    page = put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})
    sidecar = page.parent / "page-001.yaml"
    sidecar.unlink()
    outside = tmp_path / "meta.yaml"
    outside.write_text("leaked: true\n", encoding="utf-8")
    sidecar.symlink_to(outside)
    assert read_source(tmp_vault, _rel(tmp_vault, page)).meta is None


def test_the_new_errors_are_source_errors() -> None:
    for error in (SourcePathError, SourceNotFoundError, SourceFileError):
        assert issubclass(error, SourceError)
