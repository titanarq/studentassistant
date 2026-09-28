"""`edit_page_transcription`: the student's hand correction of a page's transcription (#473)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from studentassistant.vault import (
    TRANSCRIPTION_EDITED_KEY,
    NoTranscriptionError,
    SecretRefused,
    SourceNotFoundError,
    SourcePathError,
    Vault,
    create_subject,
    create_topic,
    edit_page_transcription,
    list_sources,
    put_page_transcription,
    put_source,
    read_source,
    remove_source,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(32))
WHEN = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Física").slug
    return subject, create_topic(tmp_vault, subject, "Cinemática").slug


def _page(vault: Vault, topic: tuple[str, str], kind: str = "notes", **meta: object) -> str:
    path = put_source(vault, *topic, kind, "foto.jpg", JPEG, {"capture_id": "c1", **meta})
    return path.relative_to(vault.path).as_posix()


def _sidecar(vault: Vault, page: str) -> dict:
    return yaml.safe_load((vault.path / page).with_suffix(".yaml").read_text(encoding="utf-8"))


def test_the_correction_replaces_the_md_and_records_the_student(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    page = _page(tmp_vault, topic)
    put_page_transcription(tmp_vault, page, "# La velozidad\n")

    written = edit_page_transcription(
        tmp_vault, page, "# La velocidad\n\nv = dx/dt\n\n", edited_at=WHEN
    )

    assert written == (tmp_vault.path / page).with_suffix(".md")
    assert written.read_text(encoding="utf-8") == "# La velocidad\n\nv = dx/dt\n"
    record = _sidecar(tmp_vault, page)[TRANSCRIPTION_EDITED_KEY]
    assert record["by"] == "student"
    assert record["at"].startswith("2026-09-28T12:00:00")
    assert record["previous_sha256"] == hashlib.sha256(b"# La velozidad\n").hexdigest()
    # The page is still one listed source; the capture id is kept.
    listed = list_sources(tmp_vault, *topic)
    assert [s.path for s in listed] == [page]
    assert listed[0].meta is not None and listed[0].meta["capture_id"] == "c1"


def test_a_sidecar_transcription_counts_as_one_to_correct(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    page = _page(tmp_vault, topic, "book", transcription="v = dx/dt")
    edit_page_transcription(tmp_vault, page, "v = Δx/Δt")
    md = page.replace(".jpg", ".md")
    assert read_source(tmp_vault, md).content.decode() == "v = Δx/Δt\n"
    record = _sidecar(tmp_vault, page)[TRANSCRIPTION_EDITED_KEY]
    assert record["previous_sha256"] == hashlib.sha256(b"v = dx/dt").hexdigest()


def test_an_untranscribed_page_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    page = _page(tmp_vault, topic)
    with pytest.raises(NoTranscriptionError):
        edit_page_transcription(tmp_vault, page, "algo")
    assert not (tmp_vault.path / page).with_suffix(".md").exists()
    assert TRANSCRIPTION_EDITED_KEY not in _sidecar(tmp_vault, page)


def test_a_blank_text_and_a_secret_are_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    page = _page(tmp_vault, topic)
    put_page_transcription(tmp_vault, page, "texto\n")
    with pytest.raises(ValueError):
        edit_page_transcription(tmp_vault, page, "  \n ")
    with pytest.raises(SecretRefused):
        edit_page_transcription(tmp_vault, page, "sk-ant-api03-" + "a" * 90)
    assert (tmp_vault.path / page).with_suffix(".md").read_text(encoding="utf-8") == "texto\n"


def test_a_removed_page_a_derived_file_and_other_kinds_are_refused(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    page = _page(tmp_vault, topic)
    put_page_transcription(tmp_vault, page, "texto\n")
    with pytest.raises(SourceNotFoundError):
        edit_page_transcription(tmp_vault, page.replace(".jpg", ".md"), "x")
    with pytest.raises(SourceNotFoundError):
        edit_page_transcription(tmp_vault, page.replace("page-001", "page-009"), "x")
    pdf = put_source(tmp_vault, *topic, "pdf", "t.pdf", b"%PDF-1.4", {})
    with pytest.raises(SourcePathError):
        edit_page_transcription(tmp_vault, pdf.relative_to(tmp_vault.path).as_posix(), "x")
    remove_source(tmp_vault, page)
    with pytest.raises(SourceNotFoundError):
        edit_page_transcription(tmp_vault, page, "x")
    assert Path(tmp_vault.path / page).with_suffix(".md").read_text(encoding="utf-8") == "texto\n"
