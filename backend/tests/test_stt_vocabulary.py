"""Vocabulary hints (#54): subject, topic and observer concepts, deduplicated and capped."""

from __future__ import annotations

import pytest

from studentassistant.config import SttSettings
from studentassistant.protocol import VOCABULARY_HINT_MAX_CHARS, VOCABULARY_HINTS_MAX_ITEMS
from studentassistant.stt import hotwords_text, vocabulary_hints, vocabulary_hints_from_settings


def test_subject_and_topic_first_then_the_newest_concepts() -> None:
    hints = vocabulary_hints(
        subject="Historia",
        topic="La Restauración",
        concepts=["turno pacífico", "caciquismo", "sufragio censitario"],
    )
    assert hints == [
        "Historia",
        "La Restauración",
        "sufragio censitario",
        "caciquismo",
        "turno pacífico",
    ]


def test_blank_repeated_and_overlong_terms_are_skipped() -> None:
    hints = vocabulary_hints(
        subject="  Física  ",
        topic="Cinemática",
        concepts=[
            "x" * (VOCABULARY_HINT_MAX_CHARS + 1),
            "fisica",
            "  ",
            "velocidad   media",
            "CINEMÁTICA",
        ],
    )
    assert hints == ["Física", "Cinemática", "velocidad media"]


def test_the_term_cap_keeps_the_newest_concepts() -> None:
    concepts = [f"concepto {i}" for i in range(10)]
    hints = vocabulary_hints(subject="S", topic="T", concepts=concepts, max_terms=4)
    assert hints == ["S", "T", "concepto 9", "concepto 8"]


def test_the_protocol_cap_bounds_the_term_count() -> None:
    concepts = [f"c{i}" for i in range(200)]
    hints = vocabulary_hints(concepts=concepts, max_terms=1000, max_chars=10_000)
    assert len(hints) == VOCABULARY_HINTS_MAX_ITEMS


def test_the_character_cap_counts_separators_and_skips_what_does_not_fit() -> None:
    # "Historia" (8) + ", " + "abc" (3) = 13; the long concept does not fit, a later short one does.
    hints = vocabulary_hints(
        subject="Historia", concepts=["ab", "un concepto largo", "abc"], max_chars=17
    )
    assert hints == ["Historia", "abc", "ab"]
    assert len(hotwords_text(hints) or "") <= 17


@pytest.mark.parametrize(("max_terms", "max_chars"), [(0, 500), (5, 0)])
def test_hints_can_be_turned_off(max_terms: int, max_chars: int) -> None:
    assert vocabulary_hints(subject="S", max_terms=max_terms, max_chars=max_chars) == []


def test_settings_cap_the_hints() -> None:
    settings = SttSettings(vocabulary_max_terms=2)
    assert vocabulary_hints_from_settings(settings, subject="S", topic="T", concepts=["c"]) == [
        "S",
        "T",
    ]
    assert vocabulary_hints_from_settings(SttSettings(vocabulary_max_terms=0), subject="S") == []


def test_hotwords_text_joins_the_hints() -> None:
    assert hotwords_text(["a", "b c"]) == "a, b c"
    assert hotwords_text([]) is None
