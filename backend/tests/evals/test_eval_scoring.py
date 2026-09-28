"""The eval rubric: normalization, page accuracy, section agreement, notes fidelity, requests."""

from __future__ import annotations

from pathlib import Path

import pytest

from studentassistant.evals import scoring
from studentassistant.evals.scoring import (
    RequestItem,
    TriageItem,
    content_words,
    levenshtein,
    normalize,
    repeated_units,
    score_notes,
    score_page,
    score_requests,
    score_sections,
    score_triage,
    score_unique,
    units,
)
from studentassistant.sources import triage


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


OVERLAP = Path(__file__).resolve().parent.parent / "fixtures" / "overlap"


def _overlap(name: str) -> str:
    return (OVERLAP / name).read_text(encoding="utf-8")


def test_the_overlap_reference_repeats_nothing() -> None:
    assert score_unique(_overlap("reference.md")) == 1.0


def test_overlapping_captures_written_in_twice_lower_unique_only() -> None:
    # The four captures pasted one after another: every idea of pages 2 and 4 read again.
    pasted = "\n\n".join(_overlap(f"page-00{i}.md") for i in range(1, 5))
    repeated = repeated_units(units(pasted))
    assert repeated == [
        "Elementos de la comunicación:",
        "Emisor: el que envía el mensaje.",
        "Receptor: el que recibe el mensaje.",
        "Mensaje: la información transmitida.",
        "Canal: medio físico por el que viaja el mensaje, p.",
        "las ondas sonoras.",
        "Contexto: situación en la que se da la comunicación.",
        "Elementos: emisor, receptor, mensaje, canal, código, contexto.",
        "Si no hay un código común, no hay comunicación.",
    ]
    assert len(units(pasted)) == 19
    assert score_unique(pasted) == round(10 / 19, 4)
    # Nothing is lost and nothing is made up, so kept and supported cannot see it.
    fidelity = score_notes(_overlap("reference.md"), pasted, [pasted])
    assert (fidelity.kept, fidelity.supported) == (1.0, 1.0)
    assert fidelity.unique == round(10 / 19, 4) and fidelity.repeated == repeated


def test_different_ideas_sharing_words_are_no_repetition() -> None:
    notes = (
        "- Emisor: quien envía el mensaje.\n- Receptor: quien recibe el mensaje.\n"
        "- Elementos: emisor, receptor, mensaje, canal, código y contexto.\n"
    )
    assert repeated_units(units(notes)) == [] and score_unique(notes) == 1.0


def test_empty_notes_repeat_nothing() -> None:
    assert score_unique("") == 1.0


def _request(kind: str, *segments: str, summary: str = "") -> RequestItem:
    return RequestItem(kind=kind, segments=list(segments), summary=summary or kind, text="")


def test_requests_detected_exactly_score_one() -> None:
    reference = [_request("edit", "seg-2"), _request("prepare_notes", "seg-5", "seg-6")]
    detected = [_request("prepare_notes", "seg-6"), _request("edit", "seg-1", "seg-2")]
    score = score_requests(reference, detected)
    assert (score.precision, score.recall, score.f1, score.matched) == (1.0, 1.0, 1.0, 2)
    assert score.missed == [] and score.spurious == []
    assert [(k.kind, k.f1) for k in score.per_kind] == [("edit", 1.0), ("prepare_notes", 1.0)]


def test_a_missed_request_lowers_the_recall() -> None:
    reference = [_request("edit", "seg-2"), _request("question", "seg-4", summary="¿por qué?")]
    score = score_requests(reference, [_request("edit", "seg-2")])
    assert (score.precision, score.recall) == (1.0, 0.5)
    assert score.f1 == pytest.approx(2 / 3, abs=1e-3)
    assert [r.summary for r in score.missed] == ["¿por qué?"] and score.spurious == []
    question = next(k for k in score.per_kind if k.kind == "question")
    assert (question.reference, question.detected, question.recall, question.f1) == (1, 0, 0, 0)


def test_a_spurious_request_lowers_the_precision() -> None:
    detected = [_request("edit", "seg-2"), _request("edit", "seg-7"), _request("edit", "seg-2")]
    score = score_requests([_request("edit", "seg-2")], detected)
    # Each reference request matches once: the second detection of seg-2 is spurious too.
    assert (score.matched, score.precision, score.recall) == (
        1,
        pytest.approx(1 / 3, abs=1e-3),
        1.0,
    )
    assert [r.segments for r in score.spurious] == [["seg-7"], ["seg-2"]]


def test_the_wrong_kind_is_both_missed_and_spurious() -> None:
    score = score_requests([_request("edit", "seg-2")], [_request("question", "seg-2")])
    assert (score.matched, score.precision, score.recall, score.f1) == (0, 0.0, 0.0, 0.0)
    assert [r.kind for r in score.missed] == ["edit"]
    assert [r.kind for r in score.spurious] == ["question"]


def test_no_requests_at_all_is_a_perfect_score() -> None:
    score = score_requests([], [])
    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0) and score.per_kind == []


def _item(capture_id: str, status: str, *reasons: str) -> TriageItem:
    return TriageItem(capture_id=capture_id, status=status, reasons=list(reasons))


def test_the_scored_reasons_are_the_triage_reasons() -> None:
    assert scoring.TRIAGE_REASONS == triage.TRIAGE_REASONS


def test_triage_scores_set_aside_captures_and_each_reason() -> None:
    reference = [
        _item("page", "kept"),
        _item("blank", "set_aside", "blank"),
        _item("again", "set_aside", "duplicate"),
        _item("blurred", "set_aside", "blurry"),
    ]
    observed = {
        "page": _item("page", "set_aside", "blurry"),  # set aside wrongly
        "blank": _item("blank", "set_aside", "blank"),
        "again": _item("again", "set_aside", "same_content"),  # right call, wrong reason
        # "blurred" was never stored: it counts as kept.
    }

    score = score_triage(reference, observed)

    assert (score.captures, score.reference_set_aside, score.set_aside, score.matched) == (
        4,
        3,
        3,
        2,
    )
    assert score.precision == pytest.approx(2 / 3, abs=1e-3)
    assert score.recall == pytest.approx(2 / 3, abs=1e-3)
    assert score.f1 == pytest.approx(2 / 3, abs=1e-3)
    by_reason = {r.reason: r for r in score.per_reason}
    assert list(by_reason) == ["blank", "duplicate", "blurry", "partial", "same_content"]
    assert by_reason["blank"].accuracy == 1.0
    assert by_reason["duplicate"].accuracy == 0.75 and by_reason["duplicate"].matched == 0
    # "page" got blurry it should not have, "blurred" lacks the one it should: two of four wrong.
    assert by_reason["blurry"].accuracy == 0.5
    assert (by_reason["blurry"].reference, by_reason["blurry"].detected) == (1, 1)
    assert by_reason["partial"].accuracy == 1.0
    assert by_reason["same_content"].accuracy == 0.75
    assert score.reason_accuracy == pytest.approx((1 + 0.75 + 0.5 + 1 + 0.75) / 5, abs=1e-3)
    assert [(wanted.capture_id, got.status) for wanted, got in score.mismatches] == [
        ("page", "set_aside"),
        ("again", "set_aside"),
        ("blurred", "missing"),
    ]


def test_triage_with_nothing_to_set_aside_and_nothing_set_aside_is_perfect() -> None:
    score = score_triage([_item("page", "kept")], {"page": _item("page", "kept")})
    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0)
    assert score.reason_accuracy == 1.0 and score.mismatches == []
    assert score_triage([], {}).reason_accuracy is None
