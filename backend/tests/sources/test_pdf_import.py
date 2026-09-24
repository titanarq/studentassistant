"""PDF import: page ranges, per-page text and thumbnails, size policy, the document block."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pymupdf
import pytest
from pdf_samples import encrypted_pdf, make_pdf

from studentassistant.config import SourcesSettings
from studentassistant.sources import (
    PageRange,
    PageRangeError,
    PdfImportError,
    PdfTooLargeError,
    PdfUnreadableError,
    citation_source_id,
    import_pdf,
    original_page,
    parse_page_range,
    pdf_document_block,
    pdf_has_page,
    pdf_page_count,
)
from studentassistant.vault import (
    SecretRefused,
    Vault,
    create_subject,
    create_topic,
    list_sources,
    read_source,
    sources_directory,
)

NOW = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Historia").slug
    return subject, create_topic(tmp_vault, subject, "La Revolución Industrial").slug


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("82-94", PageRange(82, 94)),
        ("82–94", PageRange(82, 94)),
        (" 82 - 94 ", PageRange(82, 94)),
        ("páginas 82-94", PageRange(82, 94)),
        ("Páginas 82 a 94", PageRange(82, 94)),
        ("pág. 7", PageRange(7, 7)),
        ("12", PageRange(12, 12)),
    ],
)
def test_page_ranges_are_parsed_as_the_student_writes_them(text: str, expected: PageRange) -> None:
    assert parse_page_range(text) == expected


@pytest.mark.parametrize("text", ["", "ochenta", "82-", "94-82", "0-3", "3,5"])
def test_malformed_page_ranges_are_refused(text: str) -> None:
    with pytest.raises(PageRangeError):
        parse_page_range(text)


def test_a_page_range_is_stored_with_text_and_thumbnail_per_page(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic

    imported = import_pdf(
        tmp_vault,
        subject,
        slug,
        "Tema 4.pdf",
        make_pdf(10),
        pages=PageRange(4, 6),
        now=lambda: NOW,
    )

    directory = sources_directory(tmp_vault, subject, slug, "pdf")
    assert imported.path == directory / "page-001.pdf"
    assert imported.source_id == "sources/pdf/page-001.pdf"
    assert sorted(p.name for p in directory.iterdir()) == [
        "page-001.p001.jpg",
        "page-001.p001.txt",
        "page-001.p002.jpg",
        "page-001.p002.txt",
        "page-001.p003.jpg",
        "page-001.p003.txt",
        "page-001.pdf",
        "page-001.yaml",
    ]
    stored = pymupdf.open(stream=imported.path.read_bytes(), filetype="pdf")
    assert stored.page_count == 3
    assert "Página 4 del libro" in stored[0].get_text()
    assert [p.original_page for p in imported.pages] == [4, 5, 6]
    assert [p.source_id for p in imported.pages] == [
        "sources/pdf/page-001.pdf#page=1",
        "sources/pdf/page-001.pdf#page=2",
        "sources/pdf/page-001.pdf#page=3",
    ]
    assert "Página 6 del libro" in imported.pages[2].text_path.read_text(encoding="utf-8")
    thumbnail = pymupdf.Pixmap(imported.pages[0].image_path.read_bytes())
    assert max(thumbnail.width, thumbnail.height) == SourcesSettings().pdf_thumbnail_long_edge

    [listed] = list_sources(tmp_vault, subject, slug)
    assert listed.kind == "pdf"
    assert listed.path.endswith("sources/pdf/page-001.pdf")
    assert listed.meta is not None
    assert listed.meta["original_name"] == "Tema 4.pdf"
    assert listed.meta["imported_at"] == "2026-09-24T18:30:00Z"
    assert listed.meta["original_page_count"] == 10
    assert (listed.meta["first_page"], listed.meta["last_page"], listed.meta["page_count"]) == (
        4,
        6,
        3,
    )
    assert len(listed.meta["original_sha256"]) == 64
    assert original_page(listed.meta, 2) == 5


def test_without_a_range_every_page_is_kept_and_numbering_continues(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    import_pdf(tmp_vault, subject, slug, "a.pdf", make_pdf(2))

    second = import_pdf(tmp_vault, subject, slug, "b.pdf", make_pdf(3, blank=(2,)))

    assert second.source_id == "sources/pdf/page-002.pdf"
    assert second.meta["first_page"] == 1
    assert second.meta["last_page"] == 3
    assert [p.has_text for p in second.pages] == [True, False, True]
    assert [s.path.rsplit("/", 1)[1] for s in list_sources(tmp_vault, subject, slug)] == [
        "page-001.pdf",
        "page-002.pdf",
    ]


def test_a_range_outside_the_pdf_is_refused_and_nothing_is_written(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic

    with pytest.raises(PageRangeError, match="tiene 5 páginas"):
        import_pdf(tmp_vault, subject, slug, "a.pdf", make_pdf(5), pages=PageRange(4, 9))

    assert not sources_directory(tmp_vault, subject, slug, "pdf").exists()


def test_size_policy(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    content = make_pdf(6)

    with pytest.raises(PdfTooLargeError, match="ocupa"):
        import_pdf(
            tmp_vault, subject, slug, "a.pdf", content, settings=SourcesSettings(max_pdf_bytes=10)
        )
    with pytest.raises(PdfTooLargeError, match="rango más corto"):
        import_pdf(
            tmp_vault,
            subject,
            slug,
            "a.pdf",
            content,
            pages=PageRange(1, 5),
            settings=SourcesSettings(max_pdf_pages=4),
        )
    with pytest.raises(PdfTooLargeError, match="máximo que se guarda"):
        import_pdf(
            tmp_vault,
            subject,
            slug,
            "a.pdf",
            content,
            settings=SourcesSettings(max_stored_pdf_bytes=100),
        )
    assert not sources_directory(tmp_vault, subject, slug, "pdf").exists()

    kept = import_pdf(
        tmp_vault,
        subject,
        slug,
        "a.pdf",
        content,
        pages=PageRange(2, 5),
        settings=SourcesSettings(max_pdf_pages=4),
    )
    assert kept.meta["page_count"] == 4


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "no es un PDF"),
        (b"no soy un pdf", "no es un PDF"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "no es un PDF"),
        (encrypted_pdf(), "contraseña"),
    ],
)
def test_unreadable_and_encrypted_files_are_refused(
    tmp_vault: Vault, topic: tuple[str, str], content: bytes, message: str
) -> None:
    subject, slug = topic

    with pytest.raises(PdfUnreadableError, match=message):
        import_pdf(tmp_vault, subject, slug, "a.pdf", content)

    assert not sources_directory(tmp_vault, subject, slug, "pdf").exists()


def test_a_page_whose_text_looks_like_a_key_refuses_the_whole_import(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Tema 4")
    document.new_page().insert_text(
        (72, 72), "sk-" + "ant-" + "api03-" + "Xy7Kq2Lm9Np4Rs8Tv1Wz" * 2, fontsize=4
    )
    content = document.tobytes(deflate=True)

    with pytest.raises(SecretRefused):
        import_pdf(tmp_vault, subject, slug, "a.pdf", content)

    directory = sources_directory(tmp_vault, subject, slug, "pdf")
    assert not directory.exists() or list(directory.iterdir()) == []


def test_pdf_pages_resolve_against_the_stored_page_count(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    import_pdf(tmp_vault, subject, slug, "a.pdf", make_pdf(8), pages=PageRange(3, 5))

    assert pdf_page_count(tmp_vault, subject, slug, "sources/pdf/page-001.pdf") == 3
    assert pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-001.pdf")
    assert pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-001.pdf#page=1")
    assert pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-001.pdf#page=3")
    assert not pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-001.pdf#page=4")
    assert not pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-001.pdf#page=0")
    assert not pdf_has_page(tmp_vault, subject, slug, "sources/pdf/page-002.pdf#page=1")
    assert not pdf_has_page(tmp_vault, subject, slug, "sources/pdf/../page-001.pdf#page=1")
    assert pdf_page_count(tmp_vault, subject, slug, "sources/notes/page-001.jpg") is None


def test_the_editor_gets_a_citable_document_block(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    imported = import_pdf(
        tmp_vault, subject, slug, "Tema 4.pdf", make_pdf(100), pages=PageRange(82, 94)
    )

    block = pdf_document_block(tmp_vault, subject, slug, imported.source_id, cache=True)

    assert block["type"] == "document"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(block["source"]["data"]) == imported.path.read_bytes()
    assert block["title"] == "Tema 4.pdf"
    assert block["citations"] == {"enabled": True}
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "páginas 82-94" in block["context"]
    assert "La página 1 de este documento es la página 82 del original" in block["context"]
    assert "cache_control" not in pdf_document_block(tmp_vault, subject, slug, imported.source_id)
    read = read_source(tmp_vault, imported.path.relative_to(tmp_vault.path).as_posix())
    assert read.media_type == "application/pdf"

    with pytest.raises(PdfImportError):
        pdf_document_block(tmp_vault, subject, slug, "sources/pdf/page-009.pdf")
    with pytest.raises(PdfImportError):
        pdf_document_block(tmp_vault, subject, slug, "sources/pdf/page-001.pdf#page=2")


def test_citations_map_back_to_pdf_page_source_ids() -> None:
    citation = {
        "type": "page_location",
        "cited_text": "Página 84 del libro",
        "document_index": 0,
        "document_title": "Tema 4.pdf",
        "start_page_number": 3,
        "end_page_number": 4,
    }

    assert citation_source_id("sources/pdf/page-001.pdf", citation) == (
        "sources/pdf/page-001.pdf#page=3"
    )
    assert citation_source_id("sources/pdf/page-001.pdf#page=9", citation) == (
        "sources/pdf/page-001.pdf#page=3"
    )
    assert citation_source_id("sources/pdf/page-001.pdf", {"type": "char_location"}) is None
    assert (
        citation_source_id(
            "sources/pdf/page-001.pdf", {"type": "page_location", "start_page_number": 0}
        )
        is None
    )
