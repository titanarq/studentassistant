"""The derived SQLite index: listings, FTS5 search, incremental updates and rebuilds (#23)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from git_helpers import git
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    sources_directory,
    start_session,
    topic_directory,
)
from studentassistant.vault.index import (
    DOC_NOTES,
    DOC_PAGE,
    DOC_PDF,
    DOC_TRANSCRIPT,
    DOC_WEB,
    SNIPPET_END,
    SNIPPET_START,
    VaultIndex,
    rebuild_index,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(32))


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "index.sqlite3"


@pytest.fixture
def content(tmp_vault: Vault) -> dict[str, str]:
    """A small vault: two subjects, a session with a transcript, sources, notes, pending items."""
    biology = create_subject(tmp_vault, "Biología").slug
    cells = create_topic(tmp_vault, biology, "La célula").slug
    create_topic(tmp_vault, biology, "Genética")
    chemistry = create_subject(tmp_vault, "Química").slug
    bonds = create_topic(tmp_vault, chemistry, "Enlace químico").slug

    session = start_session(tmp_vault, biology, cells, host="pc", protocol_version="1")
    session.append_transcript(0, 1_500, "La fotosíntesis ocurre en los cloroplastos")
    session.append_transcript(1_500, 3_000, "La mitocondria produce energía")
    end_session(session)

    page = put_source(tmp_vault, biology, cells, "notes", "foto.jpg", JPEG, {"capture_id": "c1"})
    page.with_suffix(".md").write_text("# Orgánulos\nEl núcleo guarda el ADN.\n", encoding="utf-8")
    web = put_source(
        tmp_vault,
        chemistry,
        bonds,
        "web",
        "Enlace iónico",
        "El enlace iónico une un metal y un no metal.",
        {"url": "https://example.invalid/ionico"},
    )
    notes = topic_directory(tmp_vault, biology, cells) / "notes" / "apuntes.md"
    notes.parent.mkdir()
    notes.write_text("# La célula\nLos cloroplastos hacen la fotosíntesis.\n", encoding="utf-8")
    review = topic_directory(tmp_vault, biology, cells) / "review" / "pending.yaml"
    review.parent.mkdir()
    review.write_text("- id: p1\n  text: ¿Mitocondria o cloroplasto?\n", encoding="utf-8")
    return {
        "biology": biology,
        "cells": cells,
        "chemistry": chemistry,
        "bonds": bonds,
        "session": session.id,
        "page": page.relative_to(tmp_vault.path).as_posix(),
        "web": web.relative_to(tmp_vault.path).as_posix(),
        "notes": notes.relative_to(tmp_vault.path).as_posix(),
    }


def _answers(index: VaultIndex) -> list[object]:
    """Everything a caller can ask the index, for comparing two indexes of the same vault."""
    return [
        index.subjects(),
        index.topics(),
        index.sessions(),
        index.sources(),
        index.pending(),
        index.note_versions(),
        index.search("fotosintesis"),
        index.search("enlace"),
        index.search("la"),
        index.search("mitocondria", kinds=[DOC_TRANSCRIPT]),
    ]


def test_listings_come_from_the_vault(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert [(s.slug, s.name) for s in index.subjects()] == [
            ("biologia", "Biología"),
            ("quimica", "Química"),
        ]
        assert [(t.subject, t.slug) for t in index.topics()] == [
            ("biologia", "genetica"),
            ("biologia", "la-celula"),
            ("quimica", "enlace-quimico"),
        ]
        assert [t.slug for t in index.topics("quimica")] == ["enlace-quimico"]
        [session] = index.sessions()
        assert (session.subject, session.topic, session.id) == (
            "biologia",
            "la-celula",
            content["session"],
        )
        assert session.ended_at is not None
        assert [(s.kind, s.path, s.meta) for s in index.sources()] == [
            ("notes", content["page"], {"capture_id": "c1"}),
            ("web", content["web"], {"url": "https://example.invalid/ionico"}),
        ]
        [pending] = index.pending("biologia", "la-celula")
        assert pending.item == {"id": "p1", "text": "¿Mitocondria o cloroplasto?"}
        assert index_path.is_file()


def test_search_finds_every_kind_with_vault_relative_ids(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    with VaultIndex.open(tmp_vault, index_path) as index:
        hits = index.search("fotosintesis")  # no accent: diacritics are ignored
        assert {hit.kind for hit in hits} == {DOC_NOTES, DOC_TRANSCRIPT}
        transcript = next(hit for hit in hits if hit.kind == DOC_TRANSCRIPT)
        assert transcript.path == (
            f"subjects/biologia/topics/la-celula/sessions/{content['session']}/transcript.jsonl"
        )
        assert (transcript.session, transcript.seq, transcript.t_start) == (
            content["session"],
            1,
            0,
        )
        assert f"{SNIPPET_START}fotosíntesis{SNIPPET_END}" in transcript.snippet
        notes = next(hit for hit in hits if hit.kind == DOC_NOTES)
        assert (notes.path, notes.source) == (content["notes"], None)

        [page] = index.search("nucleo ADN")
        assert page.kind == DOC_PAGE
        assert page.path == content["page"].removesuffix(".jpg") + ".md"
        assert page.source == content["page"]  # the page image the web opens

        [web] = index.search("ionico")
        assert (web.kind, web.path, web.source, web.subject) == (
            DOC_WEB,
            content["web"],
            content["web"],
            "quimica",
        )
        assert index.search("mitocon") != []  # a prefix matches


def test_pdf_page_text_is_searchable_and_cites_its_page(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    pdf = put_source(tmp_vault, "quimica", "enlace-quimico", "pdf", "t.pdf", b"%PDF", {})
    (pdf.parent / f"{pdf.stem}.p002.txt").write_text("Electronegatividad de Pauling", "utf-8")
    with VaultIndex.open(tmp_vault, index_path) as index:
        [hit] = index.search("pauling")
        assert hit.kind == DOC_PDF
        assert hit.source == f"{pdf.relative_to(tmp_vault.path).as_posix()}#page=2"
        assert index.search("pauling", kinds=[DOC_PAGE]) == []


def test_search_filters_and_never_takes_fts_syntax(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.search("enlace", subject="biologia") == []
        assert [h.kind for h in index.search("cloroplastos", kinds=[DOC_NOTES])] == [DOC_NOTES]
        assert index.search("cloroplastos", topic="genetica") == []
        assert index.search("   ¿? ") == []
        assert index.search('"fotosíntesis" OR (NEAR* AND') == []
        assert len(index.search("la", limit=1)) == 1
        with pytest.raises(ValueError):
            index.search("la", kinds=["slides"])
        with pytest.raises(ValueError):
            index.search("la", limit=0)


def test_deleting_the_index_and_rebuilding_gives_identical_answers(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    # Build the first index incrementally, the way a running backend does.
    with VaultIndex.open(tmp_vault, index_path) as index:
        session = start_session(tmp_vault, "quimica", "enlace-quimico", "pc", "1")
        session.append_transcript(0, 900, "El enlace covalente comparte electrones")
        index.update()
        put_source(tmp_vault, "quimica", "enlace-quimico", "pdf", "tema.pdf", b"%PDF", {"pages": 2})
        index.update()
        before = _answers(index)
    assert any(hit.kind == DOC_TRANSCRIPT for hit in before[7])  # type: ignore[attr-defined]

    index_path.unlink()
    report = rebuild_index(tmp_vault, index_path)

    assert report.rebuilt
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert _answers(index) == before


def test_update_rereads_only_what_changed(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.update().units_indexed == 0

        session = start_session(tmp_vault, content["biology"], "genetica", "pc", "1")
        report = index.update()
        # The new session's unit, and the topic whose `sessions` list it was appended to.
        assert report.units_indexed == 2
        session.append_transcript(0, 800, "Los genes dominantes y recesivos")
        report = index.update()
        assert report.units_indexed == 1
        assert [h.session for h in index.search("recesivos")] == [session.id]

        page = tmp_vault.path / content["page"]
        page.unlink()
        page.with_suffix(".md").unlink()
        page.with_suffix(".yaml").unlink()
        report = index.update()
        assert report.units_removed == 1
        assert index.search("nucleo") == []
        assert [s.kind for s in index.sources()] == ["web"]


def test_an_unreadable_file_leaves_its_unit_out_and_nothing_else(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    topic_yaml = topic_directory(tmp_vault, "quimica", "enlace-quimico") / "topic.yaml"
    topic_yaml.write_text("not: [a, topic\n", encoding="utf-8")

    with VaultIndex.open(tmp_vault, index_path) as index:
        report = index.rebuild()
        assert [unit for unit, _ in report.skipped] == ["topic/quimica/enlace-quimico"]
        assert [t.slug for t in index.topics("quimica")] == []
        assert index.search("ionico") != []  # its sources are still indexed

        topic_yaml.write_text(
            "title: Enlace químico\nfidelity_mode: estricto\ncreated_at: 2026-09-24T10:00:00Z\n"
            "sessions: []\n",
            encoding="utf-8",
        )
        assert index.update().skipped == ()
        assert [t.slug for t in index.topics("quimica")] == ["enlace-quimico"]


def test_a_new_head_triggers_a_rebuild_on_open(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    git(tmp_vault.path, "add", "-A")
    git(tmp_vault.path, "commit", "-q", "-m", "primero")
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.is_current()

    # What a pull or a clone looks like to the index: HEAD moved while it was not looking.
    (tmp_vault.path / "subjects" / "biologia" / "subject.yaml").write_text(
        "name: Biología celular\nstyle_guide: null\n", encoding="utf-8"
    )
    tag = GitSync(tmp_vault).create_notes_tag("biologia", "la-celula")

    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.is_current()
        assert index.subjects()[0].name == "Biología celular"
        [version] = index.note_versions("biologia", "la-celula")
        assert (version.subject, version.topic, version.version) == ("biologia", "la-celula", 1)
        assert version.name == tag.name == "biologia/la-celula/apuntes-v1"
        assert version.commit == git(tmp_vault.path, "rev-parse", "HEAD").strip()


def test_note_versions_of_two_subjects_sharing_a_topic_slug_stay_apart(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    create_topic(tmp_vault, "biologia", "Introducción")
    create_topic(tmp_vault, "quimica", "Introducción")
    sync = GitSync(tmp_vault)
    sync.create_notes_tag("biologia", "introduccion")
    sync.create_notes_tag("biologia", "introduccion")
    sync.create_notes_tag("quimica", "introduccion")
    # A tag in the old topic-only form is not a notes version.
    git(tmp_vault.path, "tag", "-a", "introduccion/apuntes-v9", "-m", "old")

    with VaultIndex.open(tmp_vault, index_path) as index:
        assert [(v.subject, v.version) for v in index.note_versions(topic="introduccion")] == [
            ("biologia", 1),
            ("biologia", 2),
            ("quimica", 1),
        ]
        assert [v.name for v in index.note_versions("quimica", "introduccion")] == [
            "quimica/introduccion/apuntes-v1"
        ]


def test_open_reports_a_rebuild_only_when_head_moved(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert not index.refresh().rebuilt
        git(tmp_vault.path, "add", "-A")
        git(tmp_vault.path, "commit", "-q", "-m", "x")
        assert not index.is_current()
        assert index.refresh().rebuilt
        assert index.is_current()


def test_an_index_of_another_vault_or_a_corrupt_file_is_rebuilt(
    tmp_vault: Vault, content: dict[str, str], index_path: Path, tmp_path: Path
) -> None:
    other = Vault.init(tmp_path / "other", student="Luis")
    with VaultIndex.open(other, index_path) as index:
        assert index.subjects() == []
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert len(index.subjects()) == 2

    index_path.write_bytes(b"this is not a database" * 100)
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert len(index.subjects()) == 2
    index_path.write_bytes(b"still not a database" * 100)
    assert rebuild_index(tmp_vault, index_path).documents > 0


def test_run_keeps_the_index_updated(
    tmp_vault: Vault, content: dict[str, str], index_path: Path
) -> None:
    async def scenario(index: VaultIndex) -> list[str]:
        task = asyncio.create_task(index.run(interval=0.01))
        try:
            put_source(
                tmp_vault, "quimica", "enlace-quimico", "web", "Metálico", "Mar de electrones", {}
            )
            async with asyncio.timeout(5):
                while not index.search("electrones"):
                    await asyncio.sleep(0.01)
            return [hit.kind for hit in index.search("electrones")]
        finally:
            task.cancel()

    with VaultIndex.open(tmp_vault, index_path) as index:
        assert asyncio.run(scenario(index)) == [DOC_WEB]


def test_symlinks_are_never_indexed(
    tmp_vault: Vault, content: dict[str, str], index_path: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secreto externo", encoding="utf-8")
    notes = sources_directory(tmp_vault, "biologia", "la-celula", "notes")
    os.symlink(outside, notes / "page-009.md")
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.search("externo") == []


def test_cli_index_rebuild(
    tmp_vault: Vault,
    content: dict[str, str],
    index_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    monkeypatch.setenv("SA_VAULT__INDEX_PATH", str(index_path))

    result = CliRunner().invoke(cli, ["index", "rebuild"])

    assert result.exit_code == 0, result.output
    assert "Índice reconstruido" in result.output
    with VaultIndex.open(tmp_vault, index_path) as index:
        assert index.search("fotosintesis") != []

    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_path / "nowhere"))
    result = CliRunner().invoke(cli, ["index", "rebuild"])
    assert result.exit_code == 1
    assert "No se puede abrir la bóveda" in result.output
