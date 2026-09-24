"""Topics: creating one under a subject, the suffix a colliding title gets, reading them back."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from studentassistant.vault import (
    StoredTopic,
    SubjectFileError,
    SubjectNotFoundError,
    TopicError,
    TopicFileError,
    TopicNotFoundError,
    Vault,
    VaultError,
    create_subject,
    create_topic,
    get_topic,
    list_topics,
    subject_directory,
    topic_directory,
    topics_directory,
)
from studentassistant.vault.files import read_yaml
from studentassistant.vault.models import DEFAULT_FIDELITY_MODE, Topic
from studentassistant.vault.subjects import SUBJECT_FILE_NAME, SUBJECTS_DIRNAME
from studentassistant.vault.topics import TOPIC_FILE_NAME, TOPICS_DIRNAME


def topic_file(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The `topic.yaml` of a topic slug, whether or not anything has written it yet."""
    return topic_directory(vault, subject_slug, topic_slug) / TOPIC_FILE_NAME


@pytest.fixture
def subject_slug(tmp_vault: Vault) -> str:
    """The slug of the one subject `tmp_vault` holds, for the tests that put a topic in it."""
    return create_subject(tmp_vault, "Matemáticas II").slug


def test_a_subject_that_has_no_topic_yet_lists_none_and_has_no_topics_directory(
    tmp_vault: Vault, subject_slug: str
) -> None:
    assert list_topics(tmp_vault, subject_slug) == []
    assert not topics_directory(tmp_vault, subject_slug).exists()


def test_creating_a_topic_writes_its_directory_and_its_topic_yaml(
    tmp_vault: Vault, subject_slug: str
) -> None:
    before = datetime.now(UTC)
    stored = create_topic(tmp_vault, subject_slug, "Derivadas parciales")
    after = datetime.now(UTC)

    assert stored.slug == "derivadas-parciales"
    assert stored.topic.title == "Derivadas parciales"
    assert before <= stored.topic.created_at <= after
    assert stored.topic.created_at.tzinfo is not None, "the vault records when, and where from"
    assert read_yaml(topic_file(tmp_vault, subject_slug, stored.slug), Topic) == stored.topic


def test_a_topic_is_born_with_the_defaults_the_layout_gives_it(
    tmp_vault: Vault, subject_slug: str
) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Derivadas parciales")

    assert stored.topic.fidelity_mode == DEFAULT_FIDELITY_MODE == "estricto"
    assert stored.topic.sessions == [], "no session has been recorded against it yet"
    text = topic_file(tmp_vault, subject_slug, stored.slug).read_text(encoding="utf-8")
    assert "fidelity_mode: estricto\n" in text
    assert "sessions: []\n" in text, "the file itself says what it can hold"


def test_topic_yaml_writes_its_fields_in_the_order_the_layout_shows_them(
    tmp_vault: Vault, subject_slug: str
) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Derivadas parciales")

    text = topic_file(tmp_vault, subject_slug, stored.slug).read_text(encoding="utf-8")

    assert [line.split(":", 1)[0] for line in text.splitlines()] == list(Topic.model_fields)


def test_creating_a_topic_adds_only_its_own_directory_under_its_subject(
    tmp_vault: Vault, subject_slug: str
) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Derivadas parciales")

    assert sorted(entry.name for entry in subject_directory(tmp_vault, subject_slug).iterdir()) == [
        SUBJECT_FILE_NAME,
        TOPICS_DIRNAME,
    ]
    assert sorted(entry.name for entry in topics_directory(tmp_vault, subject_slug).iterdir()) == [
        stored.slug
    ]
    assert sorted(
        entry.name for entry in topic_directory(tmp_vault, subject_slug, stored.slug).iterdir()
    ) == [TOPIC_FILE_NAME], "no temporary file of the atomic write survives it"


def test_two_titles_that_slugify_alike_become_two_topics_the_second_with_a_suffix(
    tmp_vault: Vault, subject_slug: str
) -> None:
    first = create_topic(tmp_vault, subject_slug, "Derivadas")
    second = create_topic(tmp_vault, subject_slug, "derivadas")
    third = create_topic(tmp_vault, subject_slug, "DERIVADAS")

    assert [stored.slug for stored in (first, second, third)] == [
        "derivadas",
        "derivadas-2",
        "derivadas-3",
    ]
    assert sorted(entry.name for entry in topics_directory(tmp_vault, subject_slug).iterdir()) == [
        "derivadas",
        "derivadas-2",
        "derivadas-3",
    ]
    titles_on_disk = [
        read_yaml(topic_file(tmp_vault, subject_slug, stored.slug), Topic).title
        for stored in (first, second, third)
    ]
    assert titles_on_disk == ["Derivadas", "derivadas", "DERIVADAS"], (
        "each topic keeps the title and the file it was given"
    )


def test_a_topic_slug_is_unique_inside_its_own_subject_only(tmp_vault: Vault) -> None:
    algebra = create_subject(tmp_vault, "Álgebra").slug
    matematicas = create_subject(tmp_vault, "Matemáticas II").slug

    in_algebra = create_topic(tmp_vault, algebra, "Derivadas")
    in_matematicas = create_topic(tmp_vault, matematicas, "Derivadas")
    second_in_algebra = create_topic(tmp_vault, algebra, "derivadas")

    assert (in_algebra.slug, in_matematicas.slug) == ("derivadas", "derivadas")
    assert second_in_algebra.slug == "derivadas-2", (
        "the suffix counts the siblings of its own subject, not the topics of the vault"
    )
    assert topic_directory(tmp_vault, algebra, in_algebra.slug) != topic_directory(
        tmp_vault, matematicas, in_matematicas.slug
    )
    assert [stored.slug for stored in list_topics(tmp_vault, algebra)] == [
        "derivadas",
        "derivadas-2",
    ]
    assert [stored.slug for stored in list_topics(tmp_vault, matematicas)] == ["derivadas"]


def test_listing_topics_sorts_them_by_slug_not_by_when_they_were_created(
    tmp_vault: Vault, subject_slug: str
) -> None:
    vectores = create_topic(tmp_vault, subject_slug, "Vectores")
    algebra = create_topic(tmp_vault, subject_slug, "Álgebra lineal")
    derivadas = create_topic(tmp_vault, subject_slug, "Derivadas")

    assert list_topics(tmp_vault, subject_slug) == [algebra, derivadas, vectores]
    assert [stored.slug for stored in list_topics(tmp_vault, subject_slug)] == [
        "algebra-lineal",
        "derivadas",
        "vectores",
    ]


def test_get_topic_hands_back_what_create_topic_wrote(tmp_vault: Vault, subject_slug: str) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Límites")

    assert get_topic(tmp_vault, subject_slug, stored.slug) == stored


def test_get_topic_refuses_a_slug_the_subject_does_not_have(
    tmp_vault: Vault, subject_slug: str
) -> None:
    create_topic(tmp_vault, subject_slug, "Límites")

    with pytest.raises(TopicNotFoundError, match="has no topic 'derivadas'"):
        get_topic(tmp_vault, subject_slug, "derivadas")


def test_a_topic_entry_point_refuses_a_subject_the_vault_does_not_have(tmp_vault: Vault) -> None:
    with pytest.raises(SubjectNotFoundError, match="has no subject 'fisica'"):
        create_topic(tmp_vault, "fisica", "Derivadas")
    with pytest.raises(SubjectNotFoundError, match="has no subject 'fisica'"):
        list_topics(tmp_vault, "fisica")
    with pytest.raises(SubjectNotFoundError, match="has no subject 'fisica'"):
        get_topic(tmp_vault, "fisica", "derivadas")

    assert not (tmp_vault.path / SUBJECTS_DIRNAME).exists(), "nothing was written on the way out"


def test_a_topic_is_refused_when_its_subject_is_not_a_readable_one(
    tmp_vault: Vault,
) -> None:
    subject_slug = create_subject(tmp_vault, "Química").slug
    (subject_directory(tmp_vault, subject_slug) / SUBJECT_FILE_NAME).unlink()

    with pytest.raises(SubjectFileError, match=f"{SUBJECT_FILE_NAME} is missing"):
        create_topic(tmp_vault, subject_slug, "Derivadas")

    assert not topics_directory(tmp_vault, subject_slug).exists(), (
        "a topic under a directory no listing reaches is content the student cannot open"
    )


def test_a_new_topic_never_takes_the_slug_of_a_directory_that_is_already_there(
    tmp_vault: Vault, subject_slug: str
) -> None:
    half_written = topic_directory(tmp_vault, subject_slug, "derivadas")
    half_written.mkdir(parents=True)

    stored = create_topic(tmp_vault, subject_slug, "Derivadas")

    assert stored.slug == "derivadas-2"
    assert sorted(entry.name for entry in half_written.iterdir()) == []


def test_a_topic_directory_without_its_yaml_is_refused_instead_of_listed_as_a_topic(
    tmp_vault: Vault, subject_slug: str
) -> None:
    create_topic(tmp_vault, subject_slug, "Límites")
    topic_directory(tmp_vault, subject_slug, "derivadas").mkdir(parents=True)

    with pytest.raises(TopicFileError, match=f"{TOPIC_FILE_NAME} is missing"):
        get_topic(tmp_vault, subject_slug, "derivadas")
    with pytest.raises(TopicFileError, match=f"{TOPIC_FILE_NAME} is missing"):
        list_topics(tmp_vault, subject_slug)


def test_a_topic_yaml_that_is_not_yaml_is_refused(tmp_vault: Vault, subject_slug: str) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Límites")
    topic_file(tmp_vault, subject_slug, stored.slug).write_text("{ sin cerrar\n", encoding="utf-8")

    with pytest.raises(TopicFileError, match="not YAML"):
        get_topic(tmp_vault, subject_slug, stored.slug)


def test_a_topic_yaml_with_a_field_this_backend_does_not_declare_is_refused(
    tmp_vault: Vault, subject_slug: str
) -> None:
    stored = create_topic(tmp_vault, subject_slug, "Límites")
    topic_file(tmp_vault, subject_slug, stored.slug).write_text(
        "titulo: Límites\nfidelity_mode: estricto\ncreated_at: 2026-09-24T12:00:00+00:00\n"
        "sessions: []\nasignatura: matemáticas\n",
        encoding="utf-8",
    )

    with pytest.raises(TopicFileError, match="does not hold a topic"):
        get_topic(tmp_vault, subject_slug, stored.slug)


def test_create_topic_refuses_a_title_that_has_no_letter_and_no_digit_in_it(
    tmp_vault: Vault, subject_slug: str
) -> None:
    with pytest.raises(ValueError, match="cannot make a slug"):
        create_topic(tmp_vault, subject_slug, "¡¿?!")

    assert not topics_directory(tmp_vault, subject_slug).exists()


def test_a_stored_topic_is_the_slug_and_the_topic_yaml_it_was_read_from() -> None:
    stored = StoredTopic(
        slug="derivadas",
        topic=Topic(title="Derivadas", created_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC)),
    )

    assert (
        stored.slug,
        stored.topic.title,
        stored.topic.fidelity_mode,
        stored.topic.sessions,
    ) == ("derivadas", "Derivadas", "estricto", [])


def test_every_refusal_of_a_topic_is_a_vault_error_a_caller_can_catch_as_one_type() -> None:
    assert issubclass(TopicError, VaultError)
    assert issubclass(TopicNotFoundError, TopicError)
    assert issubclass(TopicFileError, TopicError)
    assert issubclass(SubjectNotFoundError, VaultError), (
        "and so is the subject refusal a topic entry point lets through"
    )
    assert issubclass(SubjectFileError, VaultError)
