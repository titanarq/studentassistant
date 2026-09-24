"""Sources: sequential numbering per kind, a `.yaml` sidecar each, and nothing left on refusal."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from secret_samples import GITHUB_TOKEN
from yaml import safe_load

from studentassistant.vault import (
    SecretRefused,
    SourceError,
    TopicNotFoundError,
    UnknownSourceKindError,
    Vault,
    create_subject,
    create_topic,
    put_source,
    sources_directory,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(200))


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Química").slug
    return subject, create_topic(tmp_vault, subject, "Enlace químico").slug


def test_two_note_images_are_page_001_and_page_002_with_their_sidecars(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    captured = datetime(2026, 9, 24, 18, 31, tzinfo=UTC)
    first = put_source(
        tmp_vault,
        *topic,
        "notes",
        "IMG_0001.JPG",
        JPEG,
        {"capture_id": "c1", "captured_at": captured},
    )
    second = put_source(tmp_vault, *topic, "notes", "foto.jpg", JPEG[::-1], {"capture_id": "c2"})

    directory = sources_directory(tmp_vault, *topic, "notes")
    assert first == directory / "page-001.jpg"
    assert second == directory / "page-002.jpg"
    assert first.read_bytes() == JPEG
    assert second.read_bytes() == JPEG[::-1]
    assert sorted(entry.name for entry in directory.iterdir()) == [
        "page-001.jpg",
        "page-001.yaml",
        "page-002.jpg",
        "page-002.yaml",
    ]
    sidecar = (directory / "page-001.yaml").read_text(encoding="utf-8")
    assert sidecar == "capture_id: c1\ncaptured_at: '2026-09-24T18:31:00Z'\n"
    assert safe_load((directory / "page-002.yaml").read_text(encoding="utf-8")) == {
        "capture_id": "c2"
    }


def test_a_web_page_is_nnn_slug_md_with_the_slug_from_its_name(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    meta = {"url": "https://es.wikipedia.org/wiki/Enlace_químico", "fetched_at": "2026-09-24"}
    path = put_source(tmp_vault, *topic, "web", "Enlace químico — Wikipedia", "# Enlace\n", meta)
    again = put_source(tmp_vault, *topic, "web", "Enlace iónico", "# Iónico\n", meta)

    directory = sources_directory(tmp_vault, *topic, "web")
    assert path == directory / "001-enlace-quimico-wikipedia.md"
    assert again == directory / "002-enlace-ionico.md"
    assert path.read_text(encoding="utf-8") == "# Enlace\n"
    assert safe_load((directory / "001-enlace-quimico-wikipedia.yaml").read_text()) == meta


def test_numbering_resumes_after_the_pages_already_there(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    directory = sources_directory(tmp_vault, *topic, "book")
    directory.mkdir(parents=True)
    for name in ("page-001.jpg", "page-001.yaml", "page-007.page.jpg", "page-007.md"):
        (directory / name).write_bytes(b"x")

    path = put_source(tmp_vault, *topic, "book", "scan.png", b"png", {})

    assert path == directory / "page-008.png"
    assert (directory / "page-008.yaml").read_text(encoding="utf-8") == "{}\n"


def test_each_kind_numbers_on_its_own(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {})

    assert put_source(tmp_vault, *topic, "pdf", "tema.pdf", b"%PDF-1.7", {}).name == "page-001.pdf"


def test_an_unknown_kind_is_refused_with_a_typed_error(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    with pytest.raises(UnknownSourceKindError):
        put_source(tmp_vault, *topic, "video", "clase.mp4", b"x", {})

    assert issubclass(UnknownSourceKindError, SourceError)


def test_a_page_without_an_extension_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    with pytest.raises(SourceError):
        put_source(tmp_vault, *topic, "notes", "sin-extension", JPEG, {})

    assert not sources_directory(tmp_vault, *topic, "notes").exists()


def test_a_source_carrying_a_token_leaves_no_file_and_no_sidecar(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    with pytest.raises(SecretRefused):
        put_source(tmp_vault, *topic, "web", "Mis claves", f"token: {GITHUB_TOKEN}\n", {})
    with pytest.raises(SecretRefused):
        put_source(tmp_vault, *topic, "notes", "a.jpg", JPEG, {"nota": GITHUB_TOKEN})

    for kind in ("web", "notes"):
        directory = sources_directory(tmp_vault, *topic, kind)
        assert not directory.exists() or list(directory.iterdir()) == []


def test_a_source_of_an_unknown_topic_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    with pytest.raises(TopicNotFoundError):
        put_source(tmp_vault, topic[0], "no-existe", "notes", "a.jpg", JPEG, {})
