"""The exercises and mock exam generator: statements and solutions apart, Markdown and PDF."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pymupdf
import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.generators import (
    GenerateResult,
    GeneratorRegistry,
    InvalidOptionsError,
    default_registry,
    read_artifact_meta,
    run_generator,
)
from studentassistant.generators.exam import (
    EXAM_MD,
    EXAM_PDF,
    KIND,
    SOLUTIONS_MD,
    SOLUTIONS_PDF,
    TOOL_NAME,
    ExamGenerator,
    format_points,
    inline_html,
    text_html,
)
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory

_TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)


def _question(statement: str, solution: str, **fields: Any) -> dict[str, Any]:
    return {
        "statement": statement,
        "difficulty": "media",
        "solution": solution,
        "rubric": [{"criterion": "Plantea bien el límite", "points": 1}],
        "anchors": ["definicion"],
        **fields,
    }


EXERCISES = [
    _question(
        "Calcula la derivada de f(x) = x² en a = 3 con la definición.",
        "1. Planteamos el cociente.\n2. Simplificamos: **f′(3) = 6**.",
        difficulty="baja",
    ),
]
QUESTIONS = [
    _question(
        "Define la derivada de f en a.",
        "Es el límite lím_{h→0} (f(a+h) − f(a))/h.",
        points=4,
        rubric=[
            {"criterion": "Escribe el cociente incremental", "points": 2},
            {"criterion": "Toma el límite cuando h → 0", "points": 2},
        ],
    ),
    _question(
        "¿Qué regla se verá el próximo día y para qué sirve?",
        "La regla de la cadena, para derivar funciones compuestas.",
        points=6,
        rubric=[{"criterion": "Nombra la regla de la cadena", "points": 6}],
        anchors=["#proximo-dia"],
    ),
]
INSTRUCTIONS = "Responde justificando cada paso."


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


def _registry() -> GeneratorRegistry:
    registry = GeneratorRegistry()
    registry.register(ExamGenerator)
    return registry


def _generate(
    topic: ReviseTopic,
    fake: FakeClaude,
    *,
    drafted_exercises: list[dict[str, Any]] | None = None,
    drafted_questions: list[dict[str, Any]] | None = None,
    instructions: str = INSTRUCTIONS,
    **options: object,
) -> GenerateResult:
    fake.reply_tool(
        TOOL_NAME,
        {
            "instructions": instructions,
            "exercises": EXERCISES if drafted_exercises is None else drafted_exercises,
            "exam": QUESTIONS if drafted_questions is None else drafted_questions,
        },
    )
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    return _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            KIND,
            client=client,
            sync=GitSync(topic.vault),
            registry=_registry(),
            options=options,
            clock=lambda: NOW,
        )
    )


def _file(topic: ReviseTopic, name: str) -> bytes:
    return (generated_directory(topic.vault, topic.subject, topic.topic) / name).read_bytes()


def _text(topic: ReviseTopic, name: str) -> str:
    return _file(topic, name).decode("utf-8")


def _pdf_text(topic: ReviseTopic, name: str) -> tuple[int, str]:
    document = pymupdf.open("pdf", _file(topic, name))
    try:
        return document.page_count, "".join(page.get_text(flags=_TEXT_FLAGS) for page in document)
    finally:
        document.close()


def test_it_is_a_registered_generator() -> None:
    assert KIND in default_registry
    assert default_registry.lookup(KIND) is ExamGenerator
    assert ExamGenerator.title == "Ejercicios y examen"


def test_it_writes_the_statements_and_the_solutions_apart(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    result = _generate(topic, fake)

    names = [path.rsplit("/", 1)[-1] for path in result.files]
    assert sorted(names[:-1]) == sorted([EXAM_MD, SOLUTIONS_MD, EXAM_PDF, SOLUTIONS_PDF])
    assert names[-1] == f"{KIND}.meta.yaml"
    # Worked solutions (their computed numbers) are not checked, only statements and rubrics.
    assert result.ungrounded == []
    assert result.warnings == []
    assert result.items == 3

    exam = _text(topic, EXAM_MD)
    assert exam.startswith("# Ejercicios y examen: ")
    assert "## Ejercicios" in exam and "### Ejercicio 1" in exam
    assert "*Dificultad: baja*" in exam
    assert "Duración: 60 minutos · Puntuación total: 10 puntos" in exam
    assert INSTRUCTIONS in exam
    assert "### Pregunta 1 (4 puntos)" in exam
    assert "### Pregunta 2 (6 puntos)" in exam
    for question in [*EXERCISES, *QUESTIONS]:
        assert question["statement"] in exam
        assert question["solution"] not in exam
    assert "Criterio" not in exam

    solutions = _text(topic, SOLUTIONS_MD)
    for question in [*EXERCISES, *QUESTIONS]:
        assert question["solution"] in solutions
    assert "**Criterios de corrección**" in solutions
    assert "| Escribe el cociente incremental | 2 puntos |" in solutions
    assert "*Apuntes: 1. Definición*" in solutions
    assert "*Apuntes: 2. Próximo día*" in solutions


def test_the_pdfs_are_printable_and_keep_the_solutions_out_of_the_exam(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    _generate(topic, fake)

    assert _file(topic, EXAM_PDF).startswith(b"%PDF-")
    pages, exam = _pdf_text(topic, EXAM_PDF)
    assert pages >= 1
    assert "Examen de práctica" in exam
    assert "Nombre:" in exam
    assert "Define la derivada de f en a." in exam
    assert "f(x) = x²" in exam
    assert "regla de la cadena, para derivar" not in exam
    assert f"Página 1 de {pages}" in exam

    _pages, solutions = _pdf_text(topic, SOLUTIONS_PDF)
    assert "La regla de la cadena, para derivar funciones compuestas." in solutions
    assert "Toma el límite cuando h → 0" in solutions
    assert "f′(3) = 6" in solutions


def test_the_manifest_keeps_the_provenance_of_every_item(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    result = _generate(topic, fake)

    meta = read_artifact_meta(topic.vault, topic.subject, topic.topic, KIND)
    assert [(item.item, item.anchors) for item in meta.items] == [
        ("e1", ["definicion"]),
        ("p1", ["definicion"]),
        ("p2", ["proximo-dia"]),
    ]
    assert result.unresolved == []
    assert meta.options == {
        "exercises": 6,
        "questions": 5,
        "total_points": 10,
        "duration_minutes": 60,
    }


def test_the_request_carries_the_options(topic: ReviseTopic, fake: FakeClaude) -> None:
    _generate(
        topic,
        fake,
        drafted_exercises=[],
        instructions="",
        exercises=0,
        questions=2,
        duration_minutes=90,
    )

    sent = json.dumps(fake.requests[0].messages, ensure_ascii=False)
    assert "No hagas ejercicios de práctica" in sent
    assert "un examen de 2 preguntas para 90 minutos, que sume 10 puntos" in sent
    assert "#definicion" in sent
    exam = _text(topic, EXAM_MD)
    assert "## Ejercicios" not in exam
    assert "Duración: 90 minutos" in exam
    solutions = _text(topic, SOLUTIONS_MD)
    assert "## Ejercicios" not in solutions


def test_extra_items_are_cut_and_points_that_do_not_add_up_are_reported(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    questions = [
        {**QUESTIONS[0], "points": 3},
        QUESTIONS[1],
        _question("Una pregunta de más.", "Sobra.", points=1),
    ]
    result = _generate(
        topic,
        fake,
        drafted_exercises=EXERCISES * 3,
        drafted_questions=questions,
        exercises=2,
        questions=2,
    )

    assert result.warnings == [
        "Claude ha propuesto 3 ejercicios; se guardan los 2 primeros.",
        "Claude ha propuesto 3 preguntas de examen; se guardan las 2 primeras.",
        "Las preguntas del examen suman 9 puntos, no 10 puntos.",
        "Los criterios de la pregunta 1 suman 4 puntos, no 3 puntos.",
    ]
    assert result.items == 4
    assert "Una pregunta de más." not in _text(topic, EXAM_MD)


def test_a_statement_the_cited_section_does_not_hold_is_reported(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    off_topic = _question(
        "Enuncia el teorema fundamental del cálculo integral.",
        "Si F es primitiva de f continua, ∫_a^b f = F(b) − F(a) = 42.",
        points=10,
        rubric=[{"criterion": "Relaciona integral y primitiva", "points": 10}],
    )
    result = _generate(topic, fake, drafted_questions=[off_topic], questions=1)
    assert [entry.item for entry in result.ungrounded] == ["p1"]
    assert "1 elemento no se apoya claramente" in result.warnings[-1]


def test_unscored_questions_and_unknown_anchors_are_reported(
    topic: ReviseTopic, fake: FakeClaude
) -> None:
    question = {**QUESTIONS[0], "anchors": ["no-existe"]}
    del question["points"]
    result = _generate(topic, fake, drafted_exercises=[], drafted_questions=[question])

    assert "Claude no ha puntuado las preguntas 1 del examen." in result.warnings
    assert [entry.item for entry in result.unresolved] == ["p1"]
    assert "### Pregunta 1\n\nDefine" in _text(topic, EXAM_MD)


def test_invalid_options_are_refused(topic: ReviseTopic, fake: FakeClaude) -> None:
    with pytest.raises(InvalidOptionsError):
        _generate(topic, fake, questions=0)
    with pytest.raises(InvalidOptionsError):
        _generate(topic, fake, pages=3)
    assert fake.requests == []


def test_a_long_exam_takes_several_pages(topic: ReviseTopic, fake: FakeClaude) -> None:
    long = "Un enunciado largo que ocupa sitio en la página. " * 30
    questions = [_question(long, "Solución.", points=1) for _ in range(10)]
    _generate(topic, fake, drafted_exercises=[], drafted_questions=questions, questions=10)

    pages, text = _pdf_text(topic, EXAM_PDF)
    assert pages > 1
    assert f"Página {pages} de {pages}" in text


@pytest.mark.parametrize(
    ("points", "text"),
    [
        (1, "1 punto"),
        (2, "2 puntos"),
        (2.5, "2,5 puntos"),
        (0.25, "0,25 puntos"),
        (10.0, "10 puntos"),
    ],
)
def test_points_read_in_spanish(points: float, text: str) -> None:
    assert format_points(points) == text


def test_texts_become_html_blocks() -> None:
    assert inline_html("a < b y **negrita** con $x^2$") == (
        'a &lt; b y <b>negrita</b> con <span class="math">x^2</span>'
    )
    assert text_html("Uno.\nDos.\n\n- a\n- b\n\n1. primero\n2. segundo") == (
        "<p>Uno.<br/>Dos.</p><ul><li>a</li><li>b</li></ul><ol><li>primero</li><li>segundo</li></ol>"
    )
