"""The eval rubric: normalization, page accuracy, section agreement and notes fidelity."""

from __future__ import annotations

import pytest

from studentassistant.evals.scoring import (
    content_words,
    levenshtein,
    normalize,
    score_notes,
    score_page,
    score_sections,
    units,
)


def test_normalize_drops_markup_accents_and_unreadable_marks() -> None:
    text = "## Las **Células** {#celulas}\n- [[?núcleo]] y [libro](x.md)[^p1]"
    assert normalize(text) == "las celulas nucleo y libro"


def test_content_words_skip_stop_words_and_short_words() -> None:
    assert content_words("La célula es la unidad de los 3 reinos") == {
        "celula",
        "unidad",
        "3",
        "reinos",
    }


def test_levenshtein_on_characters_and_words() -> None:
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein(["a", "b", "c"], ["a", "c"]) == 1
    assert levenshtein("", "abc") == 3


def test_a_page_is_scored_by_character_and_word_accuracy() -> None:
    perfect = score_page("c1", "# Título\n\nUna línea.", "Título\nuna linea")
    assert (perfect.char_accuracy, perfect.word_accuracy) == (1.0, 1.0)
    typo = score_page("c1", "uno dos tres cuatro", "uno dos tres cuatrp")
    assert typo.word_accuracy == 0.75 and typo.char_accuracy > 0.9
    missing = score_page("c1", "uno dos", None)
    assert not missing.transcribed and missing.char_accuracy == missing.word_accuracy == 0.0
    garbage = score_page("c1", "uno", "algo muy distinto y mucho más largo")
    assert garbage.char_accuracy == 0.0 and garbage.word_accuracy == 0.0


def test_sections_agree_when_grouped_alike_whatever_the_ids() -> None:
    reference = [("A", ["s1", "s2"]), ("B", ["s3"])]
    same = score_sections(reference, {"s1": "x", "s2": "x", "s3": "y", "extra": "z"})
    assert same.pairwise_agreement == 1.0 and same.coverage == 1.0
    assert (same.reference_sections, same.observer_sections) == (2, 2)


def test_unassigned_segments_lower_coverage_and_agreement() -> None:
    reference = [("A", ["s1", "s2"]), ("B", ["s3"])]
    scored = score_sections(reference, {"s1": "x"})
    assert scored.assigned == 1 and scored.coverage == pytest.approx(1 / 3, abs=1e-3)
    # s1/s2 should be together but are not; the two pairs with s3 are rightly apart.
    assert scored.pairwise_agreement == pytest.approx(2 / 3, abs=1e-3)


def test_a_single_segment_has_no_pair_to_agree_on() -> None:
    scored = score_sections([("A", ["s1"])], {})
    assert scored.pairwise_agreement is None and scored.coverage == 0.0


def test_units_are_items_and_sentences_without_headings_or_footnotes() -> None:
    notes = (
        "# Tema\n\n## 1. Parte {#parte}\n\n"
        "La célula vive. Tiene núcleo celular.[^t1]\n\n"
        "- Membrana plasmática externa.\n- Ok\n\n"
        "[^t1]: [Clase](../sessions/x/transcript.jsonl#t=00:00:00-00:00:04)\n"
    )
    assert units(notes) == [
        "La célula vive.",
        "Tiene núcleo celular.",
        "Membrana plasmática externa.",
    ]


def test_notes_fidelity_counts_dropped_and_unsupported_units() -> None:
    reference = "- Las mitocondrias producen energía.\n- El núcleo guarda el ADN celular.\n"
    generated = (
        "- Las mitocondrias producen energía celular.\n- Los ribosomas fabrican proteínas.\n"
    )
    sources = ["hoy vemos que las mitocondrias producen energía"]
    fidelity = score_notes(reference, generated, sources)
    assert fidelity.dropped == ["El núcleo guarda el ADN celular."]
    assert fidelity.kept == 0.5
    assert fidelity.unsupported == ["Los ribosomas fabrican proteínas."]
    assert fidelity.supported == 0.5


def test_empty_generated_notes_keep_nothing() -> None:
    fidelity = score_notes("- Las mitocondrias producen energía.\n", "", [])
    assert fidelity.kept == 0.0 and fidelity.supported == 1.0 and fidelity.generated_units == 0
