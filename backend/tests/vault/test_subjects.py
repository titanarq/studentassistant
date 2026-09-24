"""Subjects: creating one, the suffix a colliding name gets, and reading them back."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.vault import (
    StoredSubject,
    SubjectError,
    SubjectFileError,
    SubjectNotFoundError,
    Vault,
    VaultError,
    create_subject,
    get_subject,
    list_subjects,
    subject_directory,
)
from studentassistant.vault.files import read_yaml
from studentassistant.vault.models import Subject
from studentassistant.vault.subjects import SUBJECT_FILE_NAME, SUBJECTS_DIRNAME
from studentassistant.vault.vault import GITATTRIBUTES_NAME, VAULT_META_NAME


def subject_file(vault: Vault, slug: str) -> Path:
    """The `subject.yaml` of a slug, whether or not anything has written it yet."""
    return subject_directory(vault, slug) / SUBJECT_FILE_NAME


def test_a_vault_that_has_no_subject_yet_lists_none_and_has_no_subjects_directory(
    tmp_vault: Vault,
) -> None:
    assert list_subjects(tmp_vault) == []
    assert not (tmp_vault.path / SUBJECTS_DIRNAME).exists()


def test_creating_a_subject_writes_its_directory_and_its_subject_yaml(tmp_vault: Vault) -> None:
    stored = create_subject(
        tmp_vault, "Matemáticas II", style_guide="Notación del curso: vectores en negrita."
    )

    assert stored.slug == "matematicas-ii"
    assert stored.subject == Subject(
        name="Matemáticas II", style_guide="Notación del curso: vectores en negrita."
    )
    assert read_yaml(subject_file(tmp_vault, stored.slug), Subject) == stored.subject


def test_subject_yaml_writes_its_fields_in_the_order_the_layout_shows_them(
    tmp_vault: Vault,
) -> None:
    stored = create_subject(tmp_vault, "Matemáticas II")

    text = subject_file(tmp_vault, stored.slug).read_text(encoding="utf-8")

    assert [line.split(":", 1)[0] for line in text.splitlines()] == list(Subject.model_fields)


def test_a_subject_created_without_a_style_guide_says_it_has_none(tmp_vault: Vault) -> None:
    stored = create_subject(tmp_vault, "Matemáticas II")

    assert stored.subject.style_guide is None
    assert "style_guide: null\n" in subject_file(tmp_vault, stored.slug).read_text(
        encoding="utf-8"
    ), "the file itself says what it can hold"


def test_creating_a_subject_adds_only_its_own_directory_to_the_vault(tmp_vault: Vault) -> None:
    stored = create_subject(tmp_vault, "Matemáticas II")

    assert sorted(entry.name for entry in tmp_vault.path.iterdir()) == [
        ".git",
        GITATTRIBUTES_NAME,
        SUBJECTS_DIRNAME,
        VAULT_META_NAME,
    ]
    assert sorted(entry.name for entry in subject_directory(tmp_vault, stored.slug).iterdir()) == [
        SUBJECT_FILE_NAME
    ], "no temporary file of the atomic write survives it"


def test_two_names_that_slugify_alike_become_two_subjects_the_second_with_a_suffix(
    tmp_vault: Vault,
) -> None:
    first = create_subject(tmp_vault, "Inglés")
    second = create_subject(tmp_vault, "ingles")
    third = create_subject(tmp_vault, "INGLÉS")

    assert [stored.slug for stored in (first, second, third)] == [
        "ingles",
        "ingles-2",
        "ingles-3",
    ]
    assert sorted(entry.name for entry in (tmp_vault.path / SUBJECTS_DIRNAME).iterdir()) == [
        "ingles",
        "ingles-2",
        "ingles-3",
    ]
    names_on_disk = [
        read_yaml(subject_file(tmp_vault, stored.slug), Subject).name
        for stored in (first, second, third)
    ]
    assert names_on_disk == ["Inglés", "ingles", "INGLÉS"], (
        "each subject keeps the name and the file it was given"
    )


def test_a_new_subject_never_takes_the_slug_of_a_directory_that_is_already_there(
    tmp_vault: Vault,
) -> None:
    half_written = subject_directory(tmp_vault, "ingles")
    half_written.mkdir(parents=True)

    stored = create_subject(tmp_vault, "Inglés")

    assert stored.slug == "ingles-2"
    assert sorted(entry.name for entry in half_written.iterdir()) == []


def test_listing_subjects_sorts_them_by_slug_not_by_when_they_were_created(
    tmp_vault: Vault,
) -> None:
    zoologia = create_subject(tmp_vault, "Zoología")
    algebra = create_subject(tmp_vault, "Álgebra")
    matematicas = create_subject(tmp_vault, "Matemáticas")

    assert list_subjects(tmp_vault) == [algebra, matematicas, zoologia]
    assert [stored.slug for stored in list_subjects(tmp_vault)] == [
        "algebra",
        "matematicas",
        "zoologia",
    ]


def test_get_subject_hands_back_what_create_subject_wrote(tmp_vault: Vault) -> None:
    stored = create_subject(tmp_vault, "Química", style_guide="Fórmulas con subíndices.")

    assert get_subject(tmp_vault, stored.slug) == stored


def test_get_subject_refuses_a_slug_the_vault_does_not_have(tmp_vault: Vault) -> None:
    create_subject(tmp_vault, "Química")

    with pytest.raises(SubjectNotFoundError, match="has no subject 'fisica'"):
        get_subject(tmp_vault, "fisica")


def test_get_subject_refuses_a_slug_no_subject_was_ever_created_under(tmp_vault: Vault) -> None:
    with pytest.raises(SubjectNotFoundError, match="has no subject 'fisica'"):
        get_subject(tmp_vault, "fisica")


def test_a_subject_directory_without_its_yaml_is_refused_instead_of_listed_as_a_subject(
    tmp_vault: Vault,
) -> None:
    create_subject(tmp_vault, "Química")
    subject_directory(tmp_vault, "fisica").mkdir(parents=True)

    with pytest.raises(SubjectFileError, match=f"{SUBJECT_FILE_NAME} is missing"):
        get_subject(tmp_vault, "fisica")
    with pytest.raises(SubjectFileError, match=f"{SUBJECT_FILE_NAME} is missing"):
        list_subjects(tmp_vault)


def test_a_subject_yaml_that_is_not_yaml_is_refused(tmp_vault: Vault) -> None:
    stored = create_subject(tmp_vault, "Química")
    subject_file(tmp_vault, stored.slug).write_text("{ sin cerrar\n", encoding="utf-8")

    with pytest.raises(SubjectFileError, match="not YAML"):
        get_subject(tmp_vault, stored.slug)


def test_a_subject_yaml_with_a_field_this_backend_does_not_declare_is_refused(
    tmp_vault: Vault,
) -> None:
    stored = create_subject(tmp_vault, "Química")
    subject_file(tmp_vault, stored.slug).write_text(
        "nombre: Química\nasignatura: ciencias\n", encoding="utf-8"
    )

    with pytest.raises(SubjectFileError, match="does not hold a subject"):
        get_subject(tmp_vault, stored.slug)


def test_create_subject_refuses_a_name_that_has_no_letter_and_no_digit_in_it(
    tmp_vault: Vault,
) -> None:
    with pytest.raises(ValueError, match="cannot make a slug"):
        create_subject(tmp_vault, "¡¿?!")

    assert not (tmp_vault.path / SUBJECTS_DIRNAME).exists()


def test_a_stored_subject_is_the_slug_and_the_subject_yaml_it_was_read_from() -> None:
    stored = StoredSubject(slug="quimica", subject=Subject(name="Química"))

    assert (stored.slug, stored.subject.name, stored.subject.style_guide) == (
        "quimica",
        "Química",
        None,
    )


def test_every_subject_refusal_is_a_vault_error_a_caller_can_catch_as_one_type() -> None:
    assert issubclass(SubjectError, VaultError)
    assert issubclass(SubjectNotFoundError, SubjectError)
    assert issubclass(SubjectFileError, SubjectError)
