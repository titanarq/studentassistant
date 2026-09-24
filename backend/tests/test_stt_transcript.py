"""The transcript assembler: duplicates, covered spans, restarted recognizers, late finals."""

from __future__ import annotations

from studentassistant.stt import NormalisedSegment, TranscriptAssembler


def seg(start: float, end: float, text: str, *, final: bool = True) -> NormalisedSegment:
    return NormalisedSegment(start=start, end=end, text=text, provider="web-speech", is_final=final)


def spans(assembler: TranscriptAssembler) -> list[tuple[float, float, str]]:
    return [(s.start, s.end, s.text) for s in assembler.written]


def test_finals_are_written_in_order() -> None:
    assembler = TranscriptAssembler()

    first = assembler.add(seg(0.0, 1.0, "la velocidad"))
    second = assembler.add(seg(1.2, 2.0, "es constante"))

    assert first == seg(0.0, 1.0, "la velocidad")
    assert second == seg(1.2, 2.0, "es constante")
    assert spans(assembler) == [(0.0, 1.0, "la velocidad"), (1.2, 2.0, "es constante")]


def test_partials_are_never_written() -> None:
    assembler = TranscriptAssembler()

    assert assembler.add(seg(0.0, 0.5, "la", final=False)) is None
    assert assembler.written == []


def test_blank_finals_are_dropped() -> None:
    assert TranscriptAssembler().add(seg(0.0, 0.5, "   ")) is None


def test_an_exact_duplicate_is_dropped() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 1.0, "la velocidad"))
    assembler.add(seg(1.2, 2.0, "es constante"))

    assert assembler.add(seg(0.0, 1.0, "la velocidad")) is None
    assert assembler.add(seg(1.2, 2.0, "es constante")) is None
    assert len(assembler.written) == 2


def test_a_final_inside_a_written_span_with_the_same_text_is_dropped() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 2.0, "la velocidad"))
    assembler.add(seg(3.0, 4.0, "es constante"))

    assert assembler.add(seg(0.2, 1.8, "la velocidad")) is None
    assert len(assembler.written) == 2


def test_the_same_words_said_again_later_are_kept() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 1.0, "muy bien"))

    assert assembler.add(seg(5.0, 6.0, "muy bien")) == seg(5.0, 6.0, "muy bien")


def test_a_restarted_recognizer_repeating_the_end_of_the_last_final_is_trimmed() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 3.0, "la aceleración es constante"))

    merged = assembler.add(seg(2.0, 5.0, "Es constante, en este caso"))

    assert merged == seg(3.0, 5.0, "en este caso")
    assert spans(assembler)[-1] == (3.0, 5.0, "en este caso")


def test_a_restarted_recognizer_repeating_only_old_words_is_dropped() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 3.0, "la aceleración es constante"))

    assert assembler.add(seg(1.0, 2.5, "aceleración es")) is None
    assert len(assembler.written) == 1


def test_an_overlapping_final_with_no_repeated_words_is_kept_whole() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 3.0, "la aceleración"))

    assert assembler.add(seg(2.5, 4.0, "es constante")) == seg(2.5, 4.0, "es constante")


def test_a_late_final_with_new_words_is_placed_after_the_last_written_one() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 1.0, "primero"))
    assembler.add(seg(4.0, 5.0, "tercero"))

    late = assembler.add(seg(2.0, 3.0, "segundo"))

    assert late is not None
    assert late.text == "segundo"
    assert late.start == 4.0
    assert late.end == 4.0
    starts = [s.start for s in assembler.written]
    assert starts == sorted(starts)


def test_a_late_final_repeating_the_last_written_words_is_dropped() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 1.0, "primero"))
    assembler.add(seg(4.0, 6.0, "tercero y cuarto"))

    assert assembler.add(seg(3.5, 5.0, "tercero")) is None


def test_words_are_compared_without_case_or_punctuation() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(0.0, 2.0, "¿Qué es la velocidad?"))

    merged = assembler.add(seg(1.5, 3.0, "la velocidad, es un vector"))

    assert merged is not None
    assert merged.text == "es un vector"


def test_seeded_segments_count_as_written() -> None:
    assembler = TranscriptAssembler()
    assembler.seed(seg(0.0, 1.0, "la velocidad"))

    assert assembler.add(seg(0.0, 1.0, "la velocidad")) is None
    assert assembler.add(seg(0.5, 2.0, "velocidad es constante")) == seg(1.0, 2.0, "es constante")


def test_an_overlap_is_trimmed_against_a_recent_final_not_only_the_last() -> None:
    assembler = TranscriptAssembler()
    assembler.add(seg(5.0, 6.0, "es constante"))
    assembler.add(seg(4.0, 4.8, "se mide en metros"))  # late: placed at 5.0

    merged = assembler.add(seg(5.5, 8.0, "es constante por segundo"))

    assert merged == seg(6.0, 8.0, "por segundo")
