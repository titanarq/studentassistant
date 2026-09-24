"""Slugs: Spanish names become directory names, and a name already used gets a numeric suffix."""

from __future__ import annotations

import pytest

from studentassistant.vault.slugs import slugify, unique_slug


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Matemáticas II", "matematicas-ii"),
        ("Historia de España", "historia-de-espana"),
        ("El Niño y la Montaña", "el-nino-y-la-montana"),
        ("Ángel María de Úrculo", "angel-maria-de-urculo"),
        ("¿Qué es una función?", "que-es-una-funcion"),
        ("Lengua: sintaxis, morfología y léxico", "lengua-sintaxis-morfologia-y-lexico"),
        ("Cálculo – Tema 1 (límites)", "calculo-tema-1-limites"),
        ("  Física   y\tQuímica  ", "fisica-y-quimica"),
        ("Bio_Química.2026", "bio-quimica-2026"),
        ("2.º de Bachillerato", "2-o-de-bachillerato"),
    ],
)
def test_slugify_keeps_only_lowercase_ascii_letters_digits_and_hyphens(
    name: str, slug: str
) -> None:
    assert slugify(name) == slug


def test_slugify_refuses_a_name_with_no_letter_or_digit_in_it() -> None:
    with pytest.raises(ValueError, match="cannot make a slug"):
        slugify("¿? ¡! …")


def test_unique_slug_leaves_a_free_base_alone() -> None:
    assert unique_slug("matematicas", ()) == "matematicas"
    assert unique_slug("matematicas", ["fisica", "quimica"]) == "matematicas"


def test_unique_slug_numbers_every_collision_of_the_same_name() -> None:
    taken: set[str] = set()

    first = unique_slug(slugify("Matemáticas"), taken)
    taken.add(first)
    second = unique_slug(slugify("MATEMÁTICAS"), taken)
    taken.add(second)
    third = unique_slug(slugify("matemáticas"), taken)

    assert (first, second, third) == ("matematicas", "matematicas-2", "matematicas-3")


def test_unique_slug_skips_the_suffixes_that_are_already_taken() -> None:
    taken = {"matematicas", "matematicas-2", "matematicas-3", "matematicas-5"}

    assert unique_slug("matematicas", taken) == "matematicas-4"
    assert unique_slug("matematicas-3", taken) == "matematicas-3-2"
