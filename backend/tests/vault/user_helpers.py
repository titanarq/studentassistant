"""Helpers the user tests share: a user a handle can be opened on, where its folder lives, and the
whole of the vault's layout written through its public writers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from studentassistant.vault import (
    ConversationRecord,
    LedgerEntry,
    Vault,
    append_conversation_record,
    append_ledger_entry,
    create_subject,
    create_topic,
    end_session,
    put_page_transcription,
    put_pasted_image,
    put_source,
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
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME

# A profile's `created_at` is never what these tests assert on, so it is a fixed one: a user a
# test writes is the same bytes every time, which keeps a failure about the content and not the
# clock of the machine that ran it.
CREATED_AT = "2026-10-03T09:00:00+00:00"

# What `write_everything` writes, and what a test compares its ids against. The two slugs below are
# the ones the default subject and topic give, so a test that writes with the defaults can name
# them; a caller that gives its own names gets the slugs `slugify` makes of them.
SUBJECT = "Matemáticas II"
TOPIC = "Derivadas"
GENERATED = "quiz.yaml"
GENERATED_TEXT = "preguntas: []\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(32))
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
WHEN = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)


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


def user_directory(vault: Vault, user_id: str) -> Path:
    """Where `user_id`'s folder lives in `vault`, whether or not anything has written it yet."""
    return vault.root / USERS_DIRNAME / user_id


def add_user(vault: Vault, user_id: str, name: str) -> Vault:
    """Give `vault` a user called `user_id`, and return the handle on that user's content.

    Writes the `profile.json` `for_user` looks for and nothing else: `create_user` (#546) is what
    creates a user in the product, with the id it derives from the name and the `subjects/` folder
    a clone needs, and the tests of the handle itself need a user to exist, not to be created the
    product's way.
    """
    directory = user_directory(vault, user_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / USER_PROFILE_NAME).write_text(
        json.dumps(
            {
                "id": user_id,
                "name": name,
                "email": None,
                "photo": None,
                "created_at": CREATED_AT,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return vault.for_user(user_id)


def everything_under(directory: Path) -> list[Path]:
    """Every path below `directory`, relative to it and sorted: a test's before/after of a disk."""
    return sorted(path.relative_to(directory) for path in directory.rglob("*"))


def handle_relative(vault: Vault, path: Path) -> str:
    """A path a writer of `vault` handed back as the id it is: relative to that handle's folder."""
    return path.relative_to(vault.path).as_posix()


def ledger_entry(subject: str, topic: str) -> LedgerEntry:
    """One LLM call's entry, as `ledger.py` writes it, for the subject and topic named."""
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


def write_everything(
    vault: Vault, text: str, *, subject: str = SUBJECT, topic: str = TOPIC
) -> list[Path]:
    """One call of every writer family of `docs/modules/vault.md`, on the handle it is given.

    Subjects, topics, a session with events and a transcript, a source of every kind, notes and a
    draft, generated material, the topic's state, its conversations, its ledger and its study
    records; `text` is what this content says, so two calls' content can be told apart, and
    `subject` and `topic` are the names the one topic it writes is called, so a test that needs two
    subjects calls it twice. Returns the paths the writers that return one handed back.

    Nothing here commits: the writers leave the files on disk, and the caller's `GitSync` decides
    what a commit of them is, and whether a version of them is worth a tag of its own
    (`sync.create_notes_tag`, which is the call that commits and tags).
    """
    subject_slug = create_subject(vault, subject).slug
    set_style_guide(vault, subject_slug, "Vectores en negrita.")
    topic_slug = create_topic(vault, subject_slug, topic).slug
    set_fidelity_mode(vault, subject_slug, topic_slug, "ampliado")

    session = start_session(vault, subject_slug, topic_slug, host="pc", protocol_version="1.0")
    session.append_event("session_started", "phone")
    session.append_transcript(0, 1200, text)
    end_session(session)

    page = put_source(
        vault,
        subject_slug,
        topic_slug,
        "notes",
        "foto.jpg",
        JPEG,
        {"capture_id": "c1"},
        derived={"page.jpg": JPEG},
    )
    transcription = put_page_transcription(vault, handle_relative(vault, page), f"# {text}\n")
    put_source(vault, subject_slug, topic_slug, "book", "libro.jpg", JPEG, {"capture_id": "c2"})
    set_book(vault, subject_slug, topic_slug, "Biología 2º Bachillerato")
    put_source(
        vault,
        subject_slug,
        topic_slug,
        "pdf",
        "tema.pdf",
        b"%PDF-1.4 fake",
        {"original_name": "tema.pdf"},
        derived={"p001.txt": "aceleración media", "p001.jpg": JPEG},
    )
    put_source(
        vault,
        subject_slug,
        topic_slug,
        "web",
        "Movimiento",
        f"# {text}\n",
        {"url": "https://x.invalid"},
    )
    pasted = put_pasted_image(vault, subject_slug, topic_slug, PNG, "image/png", added_at=WHEN)

    append_conversation_record(
        vault, subject_slug, topic_slug, "editor", ConversationRecord(time=WHEN, kind="context")
    )
    append_ledger_entry(vault, subject_slug, topic_slug, ledger_entry(subject_slug, topic_slug))

    return [
        page,
        transcription,
        pasted,
        write_notes(vault, subject_slug, topic_slug, f"# {text}\n"),
        write_notes_draft(vault, subject_slug, topic_slug, "un borrador"),
        write_generated(vault, subject_slug, topic_slug, GENERATED, GENERATED_TEXT),
        write_observer_snapshot(
            vault, subject_slug, topic_slug, Snapshot(last_seq={}, topics=[topic_slug])
        ),
        write_pending_review(
            vault, subject_slug, topic_slug, PendingReview(open_count=1, items=["duda"])
        ),
        write_topic_digest(vault, subject_slug, topic_slug, f"# Resumen de {text}\n"),
        append_study_record(vault, subject_slug, topic_slug, "quiz-results", QuizAttempt(score=8)),
        write_study_file(vault, subject_slug, topic_slug, "version", StudyVersion(label="v1")),
    ]
