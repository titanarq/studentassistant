"""Writer isolation on a user handle: what the writers touch stays in that user's own folder, and
the vault-relative ids they hand back are relative to it, not to the repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from user_helpers import add_user, everything_under

from studentassistant.vault import (
    ConversationRecord,
    LedgerEntry,
    Vault,
    append_conversation_record,
    append_ledger_entry,
    create_subject,
    create_topic,
    end_session,
    list_generated,
    list_sources,
    list_subjects,
    put_page_transcription,
    put_pasted_image,
    put_source,
    read_generated,
    read_notes,
    read_source,
    set_book,
    set_fidelity_mode,
    set_style_guide,
    start_session,
    write_generated,
    write_notes,
    write_notes_draft,
    write_observer_snapshot,
    write_pending_review,
    write_topic_digest,
)
from studentassistant.vault.models import VaultFileModel
from studentassistant.vault.study import append_study_record, write_study_file

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(32))
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
WHEN = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)

SUBJECT = "Matemáticas II"
TOPIC = "Derivadas"
# Both users below get these same two slugs on purpose: the point is that the same id names a
# different file for each of them.
SLUGS = ("matematicas-ii", "derivadas")
GENERATED = "quiz.yaml"
GENERATED_TEXT = "preguntas: []\n"


class Snapshot(BaseModel):
    """A stand-in for the observer's own model: `state.py` writes any `BaseModel`."""

    last_seq: dict[str, int]
    topics: list[str]


class PendingReview(BaseModel):
    open_count: int
    items: list[str]


class QuizAttempt(BaseModel):
    score: int


class StudyVersion(VaultFileModel):
    label: str


def ledger_entry(subject: str, topic: str) -> LedgerEntry:
    return LedgerEntry.model_validate(
        {
            "time": WHEN,
            "role": "editor",
            "model": "claude-sonnet-test",
            "input_tokens": 1200,
            "output_tokens": 300,
            "subject": subject,
            "topic": topic,
        }
    )


@pytest.fixture
def ana(tmp_vault: Vault) -> Vault:
    return add_user(tmp_vault, "ana-garcia", "Ana García")


@pytest.fixture
def luis(tmp_vault: Vault) -> Vault:
    return add_user(tmp_vault, "luis-martin", "Luis Martín")


def _relative(vault: Vault, path: Path) -> str:
    return path.relative_to(vault.path).as_posix()


def write_everything(vault: Vault, text: str) -> list[Path]:
    """One call of every writer family of `docs/modules/vault.md`, on the handle it is given.

    Subjects, topics, sessions, sources, notes, state, conversations, ledger and study; `text` is
    what this user's notes and web page say, so two users' content can be told apart. Returns the
    paths the writers that return one handed back.
    """
    subject = create_subject(vault, SUBJECT).slug
    set_style_guide(vault, subject, "Vectores en negrita.")
    topic = create_topic(vault, subject, TOPIC).slug
    set_fidelity_mode(vault, subject, topic, "ampliado")

    session = start_session(vault, subject, topic, host="pc", protocol_version="1.0")
    session.append_event("session_started", "phone")
    session.append_transcript(0, 1200, text)
    end_session(session)

    page = put_source(
        vault,
        subject,
        topic,
        "notes",
        "foto.jpg",
        JPEG,
        {"capture_id": "c1"},
        derived={"page.jpg": JPEG},
    )
    transcription = put_page_transcription(vault, _relative(vault, page), f"# {text}\n")
    put_source(vault, subject, topic, "book", "libro.jpg", JPEG, {"capture_id": "c2"})
    set_book(vault, subject, topic, "Biología 2º Bachillerato")
    put_source(
        vault,
        subject,
        topic,
        "pdf",
        "tema.pdf",
        b"%PDF-1.4 fake",
        {"original_name": "tema.pdf"},
        derived={"p001.txt": "aceleración media", "p001.jpg": JPEG},
    )
    put_source(
        vault, subject, topic, "web", "Movimiento", f"# {text}\n", {"url": "https://x.invalid"}
    )
    pasted = put_pasted_image(vault, subject, topic, PNG, "image/png", added_at=WHEN)

    append_conversation_record(
        vault, subject, topic, "editor", ConversationRecord(time=WHEN, kind="context")
    )
    append_ledger_entry(vault, subject, topic, ledger_entry(subject, topic))

    return [
        page,
        transcription,
        pasted,
        write_notes(vault, subject, topic, f"# {text}\n"),
        write_notes_draft(vault, subject, topic, "un borrador"),
        write_generated(vault, subject, topic, GENERATED, GENERATED_TEXT),
        write_observer_snapshot(vault, subject, topic, Snapshot(last_seq={}, topics=[topic])),
        write_pending_review(vault, subject, topic, PendingReview(open_count=1, items=["duda"])),
        write_topic_digest(vault, subject, topic, f"# Resumen de {text}\n"),
        append_study_record(vault, subject, topic, "quiz-results", QuizAttempt(score=8)),
        write_study_file(vault, subject, topic, "version", StudyVersion(label="v1")),
    ]


def test_every_writer_of_a_user_handle_writes_inside_that_users_folder(
    tmp_vault: Vault, ana: Vault
) -> None:
    before = set(everything_under(tmp_vault.root))

    paths = write_everything(ana, "las derivadas de Ana")

    added = set(everything_under(tmp_vault.root)) - before
    assert added, "the writers wrote nothing at all, so this test would pass on its own"
    outside = sorted(path for path in added if path.parts[:2] != ("users", "ana-garcia"))
    assert outside == [], "a writer given a user handle put something outside users/ana-garcia/"
    assert all(path.is_relative_to(ana.path) for path in paths)
    assert _relative(ana, paths[0]) == (
        "subjects/matematicas-ii/topics/derivadas/sources/notes/page-001.jpg"
    ), "the path a writer returns is the one relative to the user's folder"


def test_the_ids_a_user_handle_hands_back_are_relative_to_its_folder_and_read_back(
    ana: Vault,
) -> None:
    write_everything(ana, "las derivadas de Ana")

    sources = list_sources(ana, *SLUGS)
    assert sources, "no source was stored, so this test would pass on its own"
    for stored in sources:
        assert stored.path.startswith("subjects/matematicas-ii/topics/derivadas/sources/")
        assert read_source(ana, stored.path).content, f"{stored.path} is not readable back"
    assert list_generated(ana, *SLUGS) == [
        f"subjects/{SLUGS[0]}/topics/{SLUGS[1]}/generated/{GENERATED}"
    ], "generated material is listed by the same kind of id, relative to the user's folder"
    assert read_generated(ana, *SLUGS, GENERATED) == GENERATED_TEXT.encode("utf-8")


def test_two_users_with_the_same_slugs_never_see_each_others_files(
    tmp_vault: Vault, ana: Vault, luis: Vault
) -> None:
    write_everything(ana, "las derivadas de Ana")
    write_everything(luis, "las derivadas de Luis")

    assert [stored.slug for stored in list_subjects(ana)] == [SLUGS[0]]
    assert [stored.slug for stored in list_subjects(luis)] == [SLUGS[0]]
    assert read_notes(ana, *SLUGS) == "# las derivadas de Ana\n"
    assert read_notes(luis, *SLUGS) == "# las derivadas de Luis\n"

    ana_sources = [stored.path for stored in list_sources(ana, *SLUGS)]
    luis_sources = [stored.path for stored in list_sources(luis, *SLUGS)]
    assert ana_sources == luis_sources, "the same content gives the same ids to both users"
    web = next(path for path in ana_sources if "/sources/web/" in path)
    assert read_source(ana, web).content != read_source(luis, web).content, (
        "one id, two files: each user reads its own"
    )

    assert list_subjects(tmp_vault) == [], "the root handle has no content of its own"
    assert not (tmp_vault.root / "subjects").exists()
