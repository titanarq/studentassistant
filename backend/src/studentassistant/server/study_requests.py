"""Generation requests in the study chat: "hazme un quiz", "genera 20 tarjetas", "hazme ejercicios".

The study screen (epic #332) has no "Generar" button: it tells the student to ask the study chat
(«Pídelo en el chat: «hazme un quiz»»). `match_generation` recognises such a request in the text of
a written question, deterministically (no LLM call: obeying stays deterministic, ADR-0006 spirit),
before the tutor is asked (`tutor_routes.py`).

The grammar, over the text lowercased, without accents and punctuation:

    [courtesy] VERB [otra vez | de nuevo] [ARTICLE] [COUNT] MATERIAL [tail]

- VERB: `hazme`, `haz`, `genera(me)`, `prepara(me)`, `crea(me)`, `quiero`, `dame`, `da`, with or
  without accents, `me` joined or not (`haz me`);
- MATERIAL, and the generator kind (`KIND` of each generator module) and study option it names:
  `esquema(s)` -> `esquema`; `ejercicios` -> `examen` (option `ejercicios`); `examen(es)`,
  `simulacro(s)` -> `examen` (option `examen`); `quiz(zes)`, `test(s)`, `preguntas` -> `quiz`;
  `tarjetas (de memoria)`, `flashcards` -> `flashcards` (option `tarjetas`); `diapositivas` ->
  `diapositivas`;
- a COUNT is a number (digits or a Spanish word up to `cien`) before the material or before a unit
  noun in the tail (`de 10 preguntas`, `de 4 preguntas`, `5 ejercicios`), or right after the
  material (`un quiz de 10`); it sets the generator's existing option (`QuizOptions.size`,
  `FlashcardsOptions.size`, `ExamOptions.exercises` / `questions`, `SlidesOptions.size`), clamped
  to the option's bounds as the registry's options model declares them (`clamped` says so, in
  Spanish);
- a quiz difficulty in the tail: `facil(es)`, `media(s)`/`medio(s)`, `dificil(es)` ->
  `QuizOptions.difficulty` `easy`/`medium`/`hard`.

`otra vez` / `de nuevo` need nothing: a generation always replaces the material. Anything else --
questions *about* a material ("¿qué es un quiz?"), another verb ("explícame el esquema de la página
3"), a verb followed by something that is not a material ("quiero saber qué preguntas caen") -- is
no match, and the tutor answers it as before.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from studentassistant.generators import GeneratorRegistry, default_registry
from studentassistant.generators.exam import KIND as EXAM_KIND
from studentassistant.generators.flashcards import KIND as FLASHCARDS_KIND
from studentassistant.generators.outline import KIND as OUTLINE_KIND
from studentassistant.generators.quiz import KIND as QUIZ_KIND
from studentassistant.generators.slides import KIND as SLIDES_KIND

GenerationOption = Literal["esquema", "ejercicios", "examen", "quiz", "tarjetas", "diapositivas"]
"""The study option a generation fills (`StudyOptionKey` of `study_routes.py`)."""

OPTION_TITLES: dict[GenerationOption, str] = {
    "esquema": "Esquema",
    "ejercicios": "Ejercicios",
    "examen": "Examen",
    "quiz": "Quiz",
    "tarjetas": "Tarjetas de memoria",
    "diapositivas": "Diapositivas",
}
"""The study screen's title of each option (`web/src/study/options.ts`)."""

_COURTESY = {"por", "favor", "oye", "venga", "vale", "bueno", "ahora", "porfa", "porfavor", "y"}
_VERBS = {
    "hazme",
    "haz",
    "genera",
    "generame",
    "prepara",
    "preparame",
    "crea",
    "creame",
    "quiero",
    "dame",
    "da",
}
_ARTICLES = {
    "un",
    "una",
    "unos",
    "unas",
    "el",
    "la",
    "los",
    "las",
    "otro",
    "otra",
    "otros",
    "otras",
}
_AGAIN = (("otra", "vez"), ("de", "nuevo"))

# material word -> (kind, option)
_MATERIALS: dict[str, tuple[str, GenerationOption]] = {
    "esquema": (OUTLINE_KIND, "esquema"),
    "esquemas": (OUTLINE_KIND, "esquema"),
    "ejercicios": (EXAM_KIND, "ejercicios"),
    "examen": (EXAM_KIND, "examen"),
    "examenes": (EXAM_KIND, "examen"),
    "simulacro": (EXAM_KIND, "examen"),
    "simulacros": (EXAM_KIND, "examen"),
    "quiz": (QUIZ_KIND, "quiz"),
    "quizz": (QUIZ_KIND, "quiz"),
    "quizzes": (QUIZ_KIND, "quiz"),
    "quizes": (QUIZ_KIND, "quiz"),
    "test": (QUIZ_KIND, "quiz"),
    "tests": (QUIZ_KIND, "quiz"),
    "preguntas": (QUIZ_KIND, "quiz"),
    "tarjetas": (FLASHCARDS_KIND, "tarjetas"),
    "flashcards": (FLASHCARDS_KIND, "tarjetas"),
    "diapositivas": (SLIDES_KIND, "diapositivas"),
}

# (kind, unit noun) -> the option a count of that noun sets
_UNIT_FIELDS: dict[tuple[str, str], str] = {
    (QUIZ_KIND, "preguntas"): "size",
    (FLASHCARDS_KIND, "tarjetas"): "size",
    (FLASHCARDS_KIND, "flashcards"): "size",
    (EXAM_KIND, "ejercicios"): "exercises",
    (EXAM_KIND, "preguntas"): "questions",
    (SLIDES_KIND, "diapositivas"): "size",
}
# The option a bare count sets ("hazme 10 preguntas", "un quiz de 10"), per option.
_PRIMARY_FIELDS: dict[GenerationOption, str] = {
    "quiz": "size",
    "tarjetas": "size",
    "ejercicios": "exercises",
    "examen": "questions",
    "diapositivas": "size",
}
_UNIT_NAMES = {
    (QUIZ_KIND, "size"): "preguntas",
    (FLASHCARDS_KIND, "size"): "tarjetas",
    (EXAM_KIND, "exercises"): "ejercicios",
    (EXAM_KIND, "questions"): "preguntas de examen",
    (SLIDES_KIND, "size"): "diapositivas",
}
_DIFFICULTIES = {
    "facil": "easy",
    "faciles": "easy",
    "media": "medium",
    "medias": "medium",
    "medio": "medium",
    "medios": "medium",
    "dificil": "hard",
    "dificiles": "hard",
}

_UNITS = {
    "uno": 1,
    "un": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
}
_NUMBER_WORDS: dict[str, int] = {
    **{k: v for k, v in _UNITS.items() if k not in {"un", "una"}},
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiuno": 21,
    "veintiuna": 21,
    "veintidos": 22,
    "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
    "cien": 100,
}
_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70}
_TENS.update({"ochenta": 80, "noventa": 90})


@dataclass(frozen=True)
class GenerationRequest:
    """A recognised request to generate one study material."""

    kind: str
    """The generator kind (`esquema`, `examen`, `quiz`, `flashcards`, `diapositivas`)."""
    option: GenerationOption
    """The study option it fills (`ejercicios` or `examen` for kind `examen`)."""
    options: dict[str, Any] = field(default_factory=dict)
    """The generator options the text set (validated against its options model)."""
    clamped: list[str] = field(default_factory=list)
    """Spanish sentences, one per count brought within the option's bounds."""


def normalize(text: str) -> list[str]:
    """The words of `text`: lowercased, without accents or punctuation (digits kept)."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", plain)


def _number(words: list[str], at: int) -> tuple[int, int] | None:
    """The number starting at `words[at]` and how many words it takes, or None."""
    if at >= len(words):
        return None
    word = words[at]
    if word.isdigit():
        return int(word), 1
    if word in _TENS and at + 2 < len(words) and words[at + 1] == "y":
        unit = _UNITS.get(words[at + 2])
        if unit is not None:
            return _TENS[word] + unit, 3
    value = _NUMBER_WORDS.get(word)
    return (value, 1) if value is not None else None


def _bounds(registry: GeneratorRegistry, kind: str, name: str) -> tuple[int | None, int | None]:
    generator = registry.lookup(kind)
    if generator is None or name not in generator.options_model.model_fields:
        return None, None
    low: int | None = None
    high: int | None = None
    for constraint in generator.options_model.model_fields[name].metadata:
        # pydantic keeps `Field(ge=..., le=...)` as `annotated_types.Ge`/`Le` metadata.
        if isinstance(getattr(constraint, "ge", None), int | float):
            low = int(constraint.ge)
        if isinstance(getattr(constraint, "le", None), int | float):
            high = int(constraint.le)
    return low, high


def _clamp(registry: GeneratorRegistry, kind: str, name: str, value: int) -> tuple[int, str | None]:
    low, high = _bounds(registry, kind, name)
    unit = _UNIT_NAMES.get((kind, name), "elementos")
    if high is not None and value > high:
        return high, f"Como mucho pueden ser {high} {unit}: preparo {high}."
    if low is not None and value < low:
        return low, f"Como mínimo tiene que haber {low}: preparo {low}."
    return value, None


def match_generation(
    text: str, *, registry: GeneratorRegistry = default_registry
) -> GenerationRequest | None:
    """The generation `text` asks for, or None when it is not such a request (see the module
    docstring). Pure: reads only `registry`'s options models, for the counts' bounds. A material
    whose kind `registry` does not have is no match."""
    words = normalize(text)
    at = 0
    while at < len(words) and words[at] in _COURTESY:
        at += 1
    if at >= len(words) or words[at] not in _VERBS:
        return None
    at += 1
    if at < len(words) and words[at] == "me":
        at += 1

    def skip_again(position: int) -> int:
        for pair in _AGAIN:
            if tuple(words[position : position + 2]) == pair:
                return position + 2
        return position

    at = skip_again(at)
    if at < len(words) and words[at] in _ARTICLES:
        at += 1
    count: int | None = None
    number = _number(words, at)
    if number is not None and at + number[1] < len(words) and words[at + number[1]] in _MATERIALS:
        count, width = number
        at += width
    if at >= len(words) or words[at] not in _MATERIALS:
        return None
    material = words[at]
    kind, option = _MATERIALS[material]
    if kind not in registry:
        return None
    at += 1
    if material == "tarjetas" and words[at : at + 2] == ["de", "memoria"]:
        at += 2

    values: dict[str, int] = {}
    if count is not None:
        name = _UNIT_FIELDS.get((kind, material)) or _PRIMARY_FIELDS.get(option)
        if name is not None:
            values[name] = count
    difficulty: str | None = None
    tail = words[at:]
    position = 0
    while position < len(tail):
        number = _number(tail, position)
        if number is not None:
            value, width = number
            after = position + width
            unit = tail[after] if after < len(tail) else None
            name = _UNIT_FIELDS.get((kind, unit)) if unit is not None else None
            if name is None and position == 1 and tail[0] == "de":
                # "un quiz de 10": a bare count right after the material.
                name = _PRIMARY_FIELDS.get(option)
            if name is not None and name not in values:
                values[name] = value
                position = after + 1
                continue
        if tail[position] in _DIFFICULTIES:
            difficulty = difficulty or _DIFFICULTIES[tail[position]]
        position += 1

    options: dict[str, Any] = {}
    clamped: list[str] = []
    for name, value in values.items():
        bounded, note = _clamp(registry, kind, name, value)
        options[name] = bounded
        if note is not None:
            clamped.append(note)
    if kind == QUIZ_KIND and difficulty is not None:
        options["difficulty"] = difficulty
    return GenerationRequest(kind=kind, option=option, options=options, clamped=clamped)


# -- asking back (#383) ----------------------------------------------------------------------------

# The options each study option asks back for when a request sets none of them. `esquema` has no
# options: it never asks back.
_ASKED_FIELDS: dict[GenerationOption, tuple[str, ...]] = {
    "quiz": ("size", "difficulty"),
    "tarjetas": ("size",),
    "ejercicios": ("exercises",),
    "examen": ("questions",),
    "diapositivas": ("size",),
}
_DIFFICULTY_NAMES = {"easy": "fácil", "medium": "media", "hard": "difícil", "mixed": "variada"}
_COMPLETION_DIFFICULTIES = {
    **_DIFFICULTIES,
    "variada": "mixed",
    "variadas": "mixed",
    "variado": "mixed",
    "variados": "mixed",
    "mixta": "mixed",
    "mixtas": "mixed",
    "mixto": "mixed",
    "mixtos": "mixed",
}
# The unit nouns of a follow-up ("10 preguntas", "8 tarjetas"), singular too, per kind.
_COMPLETION_UNITS: dict[tuple[str, str], str] = {
    **_UNIT_FIELDS,
    (QUIZ_KIND, "pregunta"): "size",
    (FLASHCARDS_KIND, "tarjeta"): "size",
    (EXAM_KIND, "ejercicio"): "exercises",
    (EXAM_KIND, "pregunta"): "questions",
    (SLIDES_KIND, "diapositiva"): "size",
    (SLIDES_KIND, "slides"): "size",
}
# Words a follow-up may carry around its count and difficulty ("que sean 8", "de dificultad media").
_COMPLETION_FILLERS = {
    "de",
    "con",
    "que",
    "sean",
    "y",
    "pues",
    "mejor",
    "unas",
    "unos",
    "un",
    "una",
    "dificultad",
    "nivel",
    "por",
    "favor",
    "porfa",
    "venga",
    "bueno",
    "ok",
    "okay",
    "vale",
    "si",
    "hazme",
    "haz",
    "dame",
    "ponme",
    "pon",
    "quiero",
    "genera",
    "prepara",
    "memoria",
}
# A follow-up that takes the defaults ("vale", "sí", "las de por defecto", "como quieras"...).
_ACCEPTANCE_PHRASES: tuple[tuple[str, ...], ...] = (
    ("por", "defecto"),
    ("como", "quieras"),
    ("lo", "que", "quieras"),
    ("las", "que", "quieras"),
    ("los", "que", "quieras"),
    ("da", "igual"),
    ("me", "da", "igual"),
    ("tu", "mismo"),
    ("tu", "misma"),
    ("tu", "eliges"),
    ("tu", "decides"),
    ("lo", "normal"),
)
_ACCEPTANCE_WORDS = {
    "vale",
    "si",
    "ok",
    "okay",
    "venga",
    "dale",
    "perfecto",
    "claro",
    "genial",
    "bien",
    "valen",
    "bueno",
}
_COMPLETION_FILLERS |= _ACCEPTANCE_WORDS | {"las", "los", "el", "la"}


@dataclass(frozen=True)
class Clarification:
    """A study chat request that must ask back before generating: it set none of its option's
    count or difficulty (#383)."""

    kind: str
    """The generator kind to run once completed."""
    option: GenerationOption
    """The study option it will fill."""
    defaults: dict[str, Any]
    """The asked options at the generator's defaults (`{"size": 10, "difficulty": "mixed"}`)."""
    reply: str
    """The Spanish question back, naming the defaults."""


def _clarification_reply(option: GenerationOption, defaults: dict[str, Any]) -> str:
    if option == "quiz":
        size = int(defaults.get("size", 10))
        difficulty = _DIFFICULTY_NAMES.get(str(defaults.get("difficulty", "mixed")), "variada")
        return (
            "¿Cuántas preguntas quieres y de qué dificultad (fácil, media, difícil o variada)?"
            f" Si no me dices nada distinto, hago {_plural(size, 'pregunta', 'preguntas')}"
            f" de dificultad {difficulty}."
        )
    field_name = _ASKED_FIELDS[option][0]
    value = defaults.get(field_name)
    question = {
        "tarjetas": "¿Cuántas tarjetas quieres?",
        "ejercicios": "¿Cuántos ejercicios quieres?",
        "examen": "¿Cuántas preguntas quieres en el examen?",
        "diapositivas": "¿Cuántas diapositivas quieres?",
    }[option]
    return f"{question} Por defecto, {value}." if value is not None else question


def needs_parameters(
    request: GenerationRequest, *, registry: GeneratorRegistry = default_registry
) -> Clarification | None:
    """The question to ask back when `request` sets none of the count or difficulty its option
    asks for ("hazme un quiz"), or None when it can generate directly ("hazme un quiz de 5",
    "hazme un quiz difícil", any `esquema`). Pure; the defaults come from `registry`'s options
    model, as the progress line's."""
    asked = _ASKED_FIELDS.get(request.option)
    if not asked or any(name in request.options for name in asked):
        return None
    effective = _effective(registry, GenerationRequest(kind=request.kind, option=request.option))
    defaults = {name: effective[name] for name in asked if name in effective}
    return Clarification(
        kind=request.kind,
        option=request.option,
        defaults=defaults,
        reply=_clarification_reply(request.option, defaults),
    )


def _strip_acceptance(words: list[str]) -> tuple[list[str], bool]:
    """`words` without the acceptance phrases they contain, and whether there was one."""
    rest: list[str] = []
    found = False
    at = 0
    while at < len(words):
        for phrase in _ACCEPTANCE_PHRASES:
            if tuple(words[at : at + len(phrase)]) == phrase:
                at += len(phrase)
                found = True
                break
        else:
            rest.append(words[at])
            at += 1
    return rest, found


def complete_parameters(
    text: str,
    pending: Clarification,
    *,
    registry: GeneratorRegistry = default_registry,
) -> GenerationRequest | None:
    """The request a follow-up `text` completes `pending` into, or None when `text` is not such an
    answer (it then goes to the tutor). An answer is a count and/or a difficulty in any order
    ("5", "10 fáciles", "difícil, 8", "de 12", "6 ejercicios y 3 preguntas") -- the missing ones at
    their defaults, counts clamped as `match_generation`'s -- or an acceptance of the defaults
    ("vale", "sí", "las de por defecto", "como quieras", "da igual"). Pure."""
    words, accepted = _strip_acceptance(normalize(text))
    kind = pending.kind
    primary = _PRIMARY_FIELDS.get(pending.option)
    values: dict[str, int] = {}
    difficulty: str | None = None
    position = 0
    while position < len(words):
        word = words[position]
        number = _number(words, position)
        if number is not None:
            value, width = number
            after = position + width
            unit = words[after] if after < len(words) else None
            name = _COMPLETION_UNITS.get((kind, unit)) if unit is not None else None
            if name is not None:
                after += 1
            else:
                name = primary
            if name is None or name in values:
                return None
            values[name] = value
            position = after
            continue
        if word in _COMPLETION_DIFFICULTIES:
            found = _COMPLETION_DIFFICULTIES[word]
            if difficulty is not None and difficulty != found:
                return None
            difficulty = found
        elif (kind, word) in _COMPLETION_UNITS or _MATERIALS.get(word, ("",))[0] == kind:
            pass  # "preguntas fáciles", "un quiz de 5": the pending material's own nouns
        elif word not in _COMPLETION_FILLERS:
            return None  # anything else is not an answer: the tutor takes it
        position += 1
    accepted = accepted or any(word in _ACCEPTANCE_WORDS for word in words)
    if not values and difficulty is None and not accepted:
        return None  # no count, difficulty or acceptance ("vale", "sí"): not an answer

    options: dict[str, Any] = dict(pending.defaults)
    clamped: list[str] = []
    for name, value in values.items():
        bounded, note = _clamp(registry, kind, name, value)
        options[name] = bounded
        if note is not None:
            clamped.append(note)
    if kind == QUIZ_KIND and difficulty is not None:
        options["difficulty"] = difficulty
    return GenerationRequest(kind=kind, option=pending.option, options=options, clamped=clamped)


# -- the chat lines --------------------------------------------------------------------------------


def _effective(registry: GeneratorRegistry, request: GenerationRequest) -> dict[str, Any]:
    """The options the generator will run with: the defaults of its model, then the request's."""
    generator = registry.lookup(request.kind)
    if generator is None:
        return dict(request.options)
    try:
        return generator.options_model.model_validate(request.options).model_dump()
    except ValueError:
        return dict(request.options)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


_QUIZ_DIFFICULTY = {
    "easy": " fáciles",
    "medium": " de dificultad media",
    "hard": " difíciles",
    "mixed": " de dificultad variada",
}


def _what(registry: GeneratorRegistry, request: GenerationRequest) -> str:
    options = _effective(registry, request)
    if request.option == "esquema":
        return "un esquema"
    if request.option == "quiz":
        size = int(options.get("size", 10))
        difficulty = _QUIZ_DIFFICULTY.get(str(options.get("difficulty", "mixed")), "")
        return f"un quiz de {_plural(size, 'pregunta', 'preguntas')}{difficulty}"
    if request.option in ("ejercicios", "examen"):
        exercises = int(options.get("exercises", 6))
        questions = int(options.get("questions", 5))
        practice = _plural(exercises, "ejercicio", "ejercicios")
        exam = f"un examen de {_plural(questions, 'pregunta', 'preguntas')}"
        if request.option == "ejercicios":
            return f"{practice} y {exam}"
        return f"{exam} y {practice} de práctica"
    if request.option == "tarjetas":
        size = int(options.get("size", 20))
        return f"hasta {_plural(size, 'tarjeta', 'tarjetas')} de memoria"
    size = int(options.get("size", 12))
    return f"unas diapositivas (hasta {size})"


def started_text(
    request: GenerationRequest,
    notes_version: int | None,
    *,
    registry: GeneratorRegistry = default_registry,
) -> str:
    """The `generation.started` line: "Preparando un quiz de 10 preguntas con tus apuntes v4…"."""
    notes = f"tus apuntes v{notes_version}" if notes_version is not None else "tus apuntes"
    line = f"Preparando {_what(registry, request)} con {notes}…"
    if request.clamped:
        line += " " + " ".join(request.clamped)
    return line


def result_reply(request: GenerationRequest, counts: dict[str, int], items: int) -> str:
    """The `result.reply` sentence of a finished generation ("Listo: 10 preguntas. Ábrelo en
    «Quiz»."); `counts` splits an exam's items (`exercises`, `questions`) when known."""
    title = OPTION_TITLES[request.option]
    if request.option == "diapositivas":
        done = f"Listas: {_plural(items, 'diapositiva', 'diapositivas')}. Ábrelas en «{title}»."
    else:
        if request.option == "esquema":
            what = f"el esquema tiene {_plural(items, 'apartado', 'apartados')}"
        elif request.option == "quiz":
            what = _plural(items, "pregunta", "preguntas")
        elif request.option == "tarjetas":
            what = _plural(items, "tarjeta", "tarjetas")
        elif "exercises" in counts and "questions" in counts:
            practice = _plural(counts["exercises"], "ejercicio", "ejercicios")
            exam = _plural(counts["questions"], "pregunta", "preguntas")
            what = f"{practice} y un examen de {exam}"
        else:
            what = _plural(items, "ejercicio o pregunta", "ejercicios y preguntas")
        done = f"Listo: {what}. Ábrelo en «{title}»."
    if request.clamped:
        done += " " + " ".join(request.clamped)
    return done


__all__ = [
    "OPTION_TITLES",
    "Clarification",
    "GenerationOption",
    "GenerationRequest",
    "complete_parameters",
    "match_generation",
    "needs_parameters",
    "normalize",
    "result_reply",
    "started_text",
]
