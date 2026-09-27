"""The study chat's generation grammar (`server/study_requests.py`, #366)."""

from __future__ import annotations

from typing import Any

import pytest

from material_generators import points_registry
from studentassistant.server.study_requests import (
    GenerationRequest,
    match_generation,
    result_reply,
    started_text,
)

MATCHES: list[tuple[str, str, str, dict[str, Any]]] = [
    # text, kind, option, options
    ("hazme un esquema", "esquema", "esquema", {}),
    ("Haz me un esquema, por favor", "esquema", "esquema", {}),
    ("hazme ejercicios", "examen", "ejercicios", {}),
    ("prepárame 5 ejercicios", "examen", "ejercicios", {"exercises": 5}),
    ("hazme un examen", "examen", "examen", {}),
    ("un examen de 4 preguntas", None, None, {}),  # no verb: see NON_MATCHES
    ("hazme un examen de 4 preguntas", "examen", "examen", {"questions": 4}),
    ("genera un simulacro", "examen", "examen", {}),
    ("hazme un quiz", "quiz", "quiz", {}),
    ("Hazme un quiz de 10 preguntas", "quiz", "quiz", {"size": 10}),
    ("hazme un quiz de 10", "quiz", "quiz", {"size": 10}),
    ("dame un test", "quiz", "quiz", {}),
    ("quiero 12 preguntas difíciles", "quiz", "quiz", {"size": 12, "difficulty": "hard"}),
    ("hazme un quiz fácil", "quiz", "quiz", {"difficulty": "easy"}),
    ("hazme un quiz de dificultad media", "quiz", "quiz", {"difficulty": "medium"}),
    ("HAZME UN QUIZ DE DIEZ PREGUNTAS FACILES", "quiz", "quiz", {"size": 10, "difficulty": "easy"}),
    ("genera veinte tarjetas", "flashcards", "tarjetas", {"size": 20}),
    ("hazme tarjetas de memoria", "flashcards", "tarjetas", {}),
    ("créame 15 flashcards", "flashcards", "tarjetas", {"size": 15}),
    ("hazme unas diapositivas", "diapositivas", "diapositivas", {}),
    ("hazme otra vez el quiz", "quiz", "quiz", {}),
    ("genera de nuevo el esquema", "esquema", "esquema", {}),
    ("vale, hazme un examen de 3 preguntas y 2 ejercicios", "examen", "examen", {}),
]

NON_MATCHES = [
    "¿qué es un quiz?",
    "explícame el esquema de la página 3",
    "quiero saber qué preguntas caen en el examen",
    "un examen de 4 preguntas",
    "hazme una pregunta",
    "hazme un resumen",
    "¿me haces un quiz?",
    "¿Qué ejercicios hay en los apuntes?",
    "",
]


@pytest.mark.parametrize(("text", "kind", "option", "options"), MATCHES)
def test_the_grammar_table(
    text: str, kind: str | None, option: str | None, options: dict[str, Any]
) -> None:
    matched = match_generation(text)
    if kind is None:
        assert matched is None
        return
    assert matched is not None, text
    assert (matched.kind, matched.option) == (kind, option)
    if text.startswith("vale, hazme un examen"):
        assert matched.options == {"questions": 3, "exercises": 2}
    else:
        assert matched.options == options
    assert matched.clamped == []


@pytest.mark.parametrize("text", NON_MATCHES)
def test_non_matches_go_to_the_tutor(text: str) -> None:
    assert match_generation(text) is None


def test_counts_are_clamped_to_the_options_bounds() -> None:
    matched = match_generation("hazme un quiz de 50 preguntas")
    assert matched is not None and matched.options == {"size": 30}
    assert matched.clamped == ["Como mucho pueden ser 30 preguntas: preparo 30."]
    cards = match_generation("genera 500 tarjetas")
    assert cards is not None and cards.options == {"size": 100}
    none = match_generation("hazme 0 preguntas")
    assert none is not None and none.options == {"size": 1} and "mínimo" in none.clamped[0]
    reply = result_reply(matched, {}, 30)
    assert reply.startswith("Listo: 30 preguntas. Ábrelo en «Quiz».")
    assert "Como mucho" in reply


def test_a_kind_the_registry_lacks_is_no_match() -> None:
    assert match_generation("hazme un quiz", registry=points_registry()) is None


def test_the_chat_lines() -> None:
    quiz = match_generation("hazme un quiz")
    assert quiz is not None
    # A bare request uses the defaults, and says them.
    assert (
        started_text(quiz, 4)
        == "Preparando un quiz de 10 preguntas de dificultad variada con tus apuntes v4…"
    )
    assert started_text(quiz, None).endswith("con tus apuntes…")
    exercises = GenerationRequest(kind="examen", option="ejercicios")
    assert "6 ejercicios y un examen de 5 preguntas" in started_text(exercises, 1)
    assert (
        result_reply(exercises, {"exercises": 6, "questions": 1}, 7)
        == "Listo: 6 ejercicios y un examen de 1 pregunta. Ábrelo en «Ejercicios»."
    )
    exam = GenerationRequest(kind="examen", option="examen")
    assert result_reply(exam, {}, 3).endswith("Ábrelo en «Examen».")
    cards = GenerationRequest(kind="flashcards", option="tarjetas")
    assert result_reply(cards, {}, 1) == "Listo: 1 tarjeta. Ábrelo en «Tarjetas de memoria»."
    outline = GenerationRequest(kind="esquema", option="esquema")
    assert (
        result_reply(outline, {}, 4) == "Listo: el esquema tiene 4 apartados. Ábrelo en «Esquema»."
    )
    slides = GenerationRequest(kind="diapositivas", option="diapositivas")
    assert result_reply(slides, {}, 9) == "Listas: 9 diapositivas. Ábrelas en «Diapositivas»."
    assert result_reply(slides, {}, 1) == "Listas: 1 diapositiva. Ábrelas en «Diapositivas»."
