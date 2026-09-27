"""The study chat's generation grammar (`server/study_requests.py`, #366)."""

from __future__ import annotations

from typing import Any

import pytest

from material_generators import points_registry
from studentassistant.server.study_requests import (
    Clarification,
    GenerationRequest,
    complete_parameters,
    match_generation,
    needs_parameters,
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


# -- asking back (#383) ----------------------------------------------------------------------------

ASKS_BACK: list[tuple[str, str, dict[str, Any], str]] = [
    # text, option, defaults, reply
    (
        "hazme un quiz",
        "quiz",
        {"size": 10, "difficulty": "mixed"},
        "¿Cuántas preguntas quieres y de qué dificultad (fácil, media, difícil o variada)? Si no"
        " me dices nada distinto, hago 10 preguntas de dificultad variada.",
    ),
    ("hazme tarjetas", "tarjetas", {"size": 20}, "¿Cuántas tarjetas quieres? Por defecto, 20."),
    (
        "hazme ejercicios",
        "ejercicios",
        {"exercises": 6},
        "¿Cuántos ejercicios quieres? Por defecto, 6.",
    ),
    (
        "hazme un examen",
        "examen",
        {"questions": 5},
        "¿Cuántas preguntas quieres en el examen? Por defecto, 5.",
    ),
    (
        "hazme unas diapositivas",
        "diapositivas",
        {"size": 12},
        "¿Cuántas diapositivas quieres? Por defecto, 12.",
    ),
]

GENERATES_DIRECTLY = [
    "hazme un esquema",  # no options: never asks back
    "hazme un quiz de 5",
    "hazme un quiz difícil",  # the count at its default
    "hazme un quiz de 10 preguntas fáciles",
    "genera veinte tarjetas",
    "prepárame 5 ejercicios",
    "hazme un examen de 4 preguntas",
    "hazme 8 diapositivas",
]


@pytest.mark.parametrize(("text", "option", "defaults", "reply"), ASKS_BACK)
def test_a_bare_request_asks_back(
    text: str, option: str, defaults: dict[str, Any], reply: str
) -> None:
    matched = match_generation(text)
    assert matched is not None
    clarification = needs_parameters(matched)
    assert clarification == Clarification(
        kind=matched.kind, option=matched.option, defaults=defaults, reply=reply
    )


@pytest.mark.parametrize("text", GENERATES_DIRECTLY)
def test_a_request_with_its_parameters_does_not_ask(text: str) -> None:
    matched = match_generation(text)
    assert matched is not None and needs_parameters(matched) is None


def test_the_defaults_come_from_the_registry() -> None:
    from pydantic import Field

    from studentassistant.generators import GeneratorRegistry
    from studentassistant.generators.flashcards import FlashcardsGenerator, FlashcardsOptions

    class FewOptions(FlashcardsOptions):
        size: int = Field(default=7, ge=1, le=9)

    class FewCards(FlashcardsGenerator):
        options_model = FewOptions

    registry = GeneratorRegistry()
    registry.register(FewCards)
    matched = match_generation("hazme tarjetas", registry=registry)
    assert matched is not None
    clarification = needs_parameters(matched, registry=registry)
    assert clarification is not None and clarification.defaults == {"size": 7}
    assert clarification.reply.endswith("Por defecto, 7.")
    completed = complete_parameters("12", clarification, registry=registry)
    assert completed is not None and completed.options == {"size": 9}
    assert completed.clamped == ["Como mucho pueden ser 9 tarjetas: preparo 9."]


def _pending(text: str) -> Clarification:
    matched = match_generation(text)
    assert matched is not None
    clarification = needs_parameters(matched)
    assert clarification is not None
    return clarification


COMPLETIONS: list[tuple[str, str, dict[str, Any]]] = [
    # pending request, follow-up, options
    ("hazme un quiz", "5", {"size": 5, "difficulty": "mixed"}),
    ("hazme un quiz", "10 fáciles", {"size": 10, "difficulty": "easy"}),
    ("hazme un quiz", "difícil, 8", {"size": 8, "difficulty": "hard"}),
    ("hazme un quiz", "DIFICIL 8", {"size": 8, "difficulty": "hard"}),
    ("hazme un quiz", "de 12", {"size": 12, "difficulty": "mixed"}),
    ("hazme un quiz", "doce preguntas de dificultad media", {"size": 12, "difficulty": "medium"}),
    ("hazme un quiz", "fáciles", {"size": 10, "difficulty": "easy"}),
    ("hazme un quiz", "que sean variadas", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "vale", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "Sí.", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "las de por defecto", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "como quieras", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "da igual", {"size": 10, "difficulty": "mixed"}),
    ("hazme un quiz", "vale, 7", {"size": 7, "difficulty": "mixed"}),
    ("hazme tarjetas", "30 tarjetas", {"size": 30}),
    ("hazme tarjetas", "veinticinco", {"size": 25}),
    ("hazme ejercicios", "4", {"exercises": 4}),
    ("hazme ejercicios", "6 ejercicios y 3 preguntas", {"exercises": 6, "questions": 3}),
    ("hazme un examen", "8", {"questions": 8}),
    ("hazme diapositivas", "de 15", {"size": 15}),
]

NON_COMPLETIONS: list[tuple[str, str]] = [
    ("hazme un quiz", "¿Qué es la derivada?"),
    ("hazme un quiz", "no"),
    ("hazme un quiz", "de"),
    ("hazme un quiz", "¿?"),
    ("hazme un quiz", ""),
    ("hazme un quiz", "5 tarjetas"),  # another material's noun
    ("hazme un quiz", "fácil y difícil"),
    ("hazme un quiz", "5 o 6"),
    ("hazme un quiz", "10 y 12"),
    ("hazme un quiz", "sí, pero explícame antes la derivada"),
    ("hazme tarjetas", "¿cuántas caben?"),
]


@pytest.mark.parametrize(("pending", "text", "options"), COMPLETIONS)
def test_the_follow_up_table(pending: str, text: str, options: dict[str, Any]) -> None:
    clarification = _pending(pending)
    completed = complete_parameters(text, clarification)
    assert completed is not None, text
    assert (completed.kind, completed.option) == (clarification.kind, clarification.option)
    assert completed.options == options
    assert completed.clamped == []


@pytest.mark.parametrize(("pending", "text"), NON_COMPLETIONS)
def test_non_completions_go_to_the_tutor(pending: str, text: str) -> None:
    assert complete_parameters(text, _pending(pending)) is None


def test_a_follow_up_count_is_clamped() -> None:
    completed = complete_parameters("50 difíciles", _pending("hazme un quiz"))
    assert completed is not None
    assert completed.options == {"size": 30, "difficulty": "hard"}
    assert completed.clamped == ["Como mucho pueden ser 30 preguntas: preparo 30."]
    assert started_text(completed, 2) == (
        "Preparando un quiz de 30 preguntas difíciles con tus apuntes v2…"
        " Como mucho pueden ser 30 preguntas: preparo 30."
    )
