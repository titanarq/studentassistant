"""The exercises and mock exam generator (kind `examen`): printable, with solutions apart.

Claude (role `generator`, prompt `generator_exam`) turns the master notes into `exercises`
practice exercises and a mock exam of `questions` questions worth `total_points` for a sitting of
`duration_minutes`. Every exercise and question has a statement, a difficulty, a worked solution,
a grading rubric (criteria with their points) and the note anchors it came from. Four files are
written under `generated/`, the statements and the solutions apart so that the exam can be printed
without them:

- `examen.md` -- the statements: the exercises, then the exam with its duration, total points,
  instructions and the points of every question. Markdown, the source of the PDF.
- `examen-soluciones.md` -- the solutions and rubrics of both parts, with the sections of the notes
  each one comes from.
- `examen.pdf`, `examen-soluciones.pdf` -- the same, printable (A4, PyMuPDF's `Story`), the exam
  with a header for the student's name and date, room to answer every question and page numbers.

Items are `e<n>` (exercise n) and `p<n>` (exam question n) with their anchors. Points that do not
add up (the questions to `total_points`, a question's rubric to its points) are kept as Claude
gave them and reported in a Spanish warning.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
from dataclasses import dataclass
from typing import Literal

import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
    NoteSection,
)
from studentassistant.generators.registry import register
from studentassistant.llm import load_prompt

KIND = "examen"
PROMPT_NAME = "generator_exam"
TOOL_NAME = "record_exam"
EXAM_MD = f"{KIND}.md"
SOLUTIONS_MD = f"{KIND}-soluciones.md"
EXAM_PDF = f"{KIND}.pdf"
SOLUTIONS_PDF = f"{KIND}-soluciones.pdf"

_TOLERANCE = 1e-6

Difficulty = Literal["baja", "media", "alta"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExamOptions(_Strict):
    exercises: int = Field(default=6, ge=0, le=30, description="Cuántos ejercicios de práctica.")
    questions: int = Field(default=5, ge=1, le=20, description="Cuántas preguntas en el examen.")
    total_points: float = Field(
        default=10, gt=0, le=100, description="Puntuación total del examen."
    )
    duration_minutes: int = Field(
        default=60, ge=10, le=300, description="Duración del examen en minutos."
    )


class RubricCriterion(_Strict):
    criterion: str = Field(min_length=1, description="What earns the points, in Spanish.")
    points: float = Field(ge=0)


class DraftQuestion(_Strict):
    """One exercise or exam question as Claude records it."""

    statement: str = Field(min_length=1, description="The statement, in Spanish.")
    difficulty: Difficulty
    points: float | None = Field(
        default=None, gt=0, description="What the question is worth (exam questions only)."
    )
    solution: str = Field(min_length=1, description="The worked solution, in Spanish.")
    rubric: list[RubricCriterion] = Field(default_factory=list)
    anchors: list[str] = Field(
        default_factory=list, description="Anchors (no `#`) of the note sections it comes from."
    )


class DraftExam(_Strict):
    instructions: str = Field(default="", description="The exam's instructions, in Spanish.")
    exercises: list[DraftQuestion] = Field(default_factory=list)
    exam: list[DraftQuestion]


@dataclass(frozen=True)
class Question:
    """A stored exercise (`e<n>`) or exam question (`p<n>`)."""

    id: str
    number: int
    statement: str
    difficulty: Difficulty
    points: float | None
    solution: str
    rubric: list[RubricCriterion]
    anchors: list[str]


@dataclass(frozen=True)
class Exam:
    """What the files are rendered from."""

    title: str
    instructions: str
    duration_minutes: int
    total_points: float
    exercises: list[Question]
    questions: list[Question]


def format_points(points: float) -> str:
    """`2` -> `2 puntos`, `1` -> `1 punto`, `2.5` -> `2,5 puntos` (Spanish decimal comma)."""
    rounded = round(points, 2)
    text = f"{rounded:g}".replace(".", ",") if rounded != int(rounded) else str(int(rounded))
    return f"{text} punto" if rounded == 1 else f"{text} puntos"


def _questions(drafts: list[DraftQuestion], prefix: str, *, scored: bool) -> list[Question]:
    return [
        Question(
            id=f"{prefix}{number}",
            number=number,
            statement=draft.statement.strip(),
            difficulty=draft.difficulty,
            points=draft.points if scored else None,
            solution=draft.solution.strip(),
            rubric=draft.rubric,
            anchors=[anchor.lstrip("#") for anchor in draft.anchors],
        )
        for number, draft in enumerate(drafts, start=1)
    ]


def point_warnings(exam: Exam) -> list[str]:
    """Spanish warnings for exam points that do not add up."""
    warnings: list[str] = []
    missing = [q.number for q in exam.questions if q.points is None]
    if missing:
        numbers = ", ".join(str(n) for n in missing)
        warnings.append(f"Claude no ha puntuado las preguntas {numbers} del examen.")
    total = sum(q.points or 0 for q in exam.questions)
    if exam.questions and not missing and abs(total - exam.total_points) > _TOLERANCE:
        warnings.append(
            f"Las preguntas del examen suman {format_points(total)}, "
            f"no {format_points(exam.total_points)}."
        )
    for question in exam.questions:
        if question.points is None or not question.rubric:
            continue
        rubric_total = sum(criterion.points for criterion in question.rubric)
        if abs(rubric_total - question.points) > _TOLERANCE:
            warnings.append(
                f"Los criterios de la pregunta {question.number} suman "
                f"{format_points(rubric_total)}, no {format_points(question.points)}."
            )
    return warnings


# -- Markdown --------------------------------------------------------------------------------


def _heading(question: Question, word: str) -> str:
    points = f" ({format_points(question.points)})" if question.points is not None else ""
    return f"### {word} {question.number}{points}"


def _exam_header(exam: Exam) -> str:
    return (
        f"Duración: {exam.duration_minutes} minutos · "
        f"Puntuación total: {format_points(exam.total_points)}"
    )


def render_exam_markdown(exam: Exam) -> str:
    """`examen.md`: the statements only."""
    parts = [f"# Ejercicios y examen: {exam.title}\n"]
    if exam.exercises:
        parts.append("## Ejercicios\n")
        for question in exam.exercises:
            parts.append(_heading(question, "Ejercicio") + "\n")
            parts.append(f"*Dificultad: {question.difficulty}*\n")
            parts.append(question.statement + "\n")
    parts.append("## Examen de práctica\n")
    parts.append(_exam_header(exam) + "\n")
    if exam.instructions:
        parts.append(exam.instructions + "\n")
    for question in exam.questions:
        parts.append(_heading(question, "Pregunta") + "\n")
        parts.append(question.statement + "\n")
    return "\n".join(parts)


def _section_titles(question: Question, sections: dict[str, NoteSection]) -> list[str]:
    return [sections[a].title if a in sections else a for a in question.anchors]


def _rubric_markdown(question: Question) -> str:
    rows = ["| Criterio | Puntos |", "|---|---|"]
    for criterion in question.rubric:
        text = " ".join(criterion.criterion.split()).replace("|", "\\|")
        rows.append(f"| {text} | {format_points(criterion.points)} |")
    return "\n".join(rows) + "\n"


def _solution_markdown(question: Question, word: str, sections: dict[str, NoteSection]) -> str:
    lines = [_heading(question, word) + "\n", "**Solución**\n", question.solution + "\n"]
    if question.rubric:
        lines += ["**Criterios de corrección**\n", _rubric_markdown(question)]
    titles = _section_titles(question, sections)
    if titles:
        lines.append(f"*Apuntes: {'; '.join(titles)}*\n")
    return "\n".join(lines)


def render_solutions_markdown(exam: Exam, sections: dict[str, NoteSection]) -> str:
    """`examen-soluciones.md`: the solutions and rubrics of both parts."""
    parts = [f"# Soluciones: {exam.title}\n"]
    if exam.exercises:
        parts.append("## Ejercicios\n")
        parts += [_solution_markdown(q, "Ejercicio", sections) for q in exam.exercises]
    parts.append("## Examen de práctica\n")
    parts += [_solution_markdown(q, "Pregunta", sections) for q in exam.questions]
    return "\n".join(parts)


# -- HTML and PDF ----------------------------------------------------------------------------

_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_CODE = re.compile(r"`([^`\n]+)`")
_BULLET = re.compile(r"^\s*[-*]\s+")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+")


def inline_html(text: str) -> str:
    """One line of a text as HTML: escaped, `**bold**`, `*italic*`, `` `code` `` and any
    `$..$`/`$$..$$` LaTeX left as its source in a math span."""
    escaped = html.escape(text, quote=False)
    escaped = _DISPLAY_MATH.sub(
        lambda m: f'<span class="math">{m.group(1).strip()}</span>', escaped
    )
    escaped = _INLINE_MATH.sub(lambda m: f'<span class="math">{m.group(1)}</span>', escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC.sub(r"<i>\1</i>", escaped)
    return _CODE.sub(r"<code>\1</code>", escaped)


def text_html(text: str) -> str:
    """A text of paragraphs (blank-line separated) and `- `/`1. ` lists as HTML blocks."""
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(_BULLET.match(line) for line in lines):
            items = "".join(f"<li>{inline_html(_BULLET.sub('', line))}</li>" for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        elif all(_NUMBERED.match(line) for line in lines):
            items = "".join(f"<li>{inline_html(_NUMBERED.sub('', line))}</li>" for line in lines)
            blocks.append(f"<ol>{items}</ol>")
        else:
            blocks.append("<p>" + "<br/>".join(inline_html(line) for line in lines) + "</p>")
    return "".join(blocks)


_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.3; }
h1 { font-size: 17pt; margin-bottom: 4pt; }
h2 { font-size: 14pt; margin-top: 14pt; border-bottom: 1px solid #888; }
h3 { font-size: 12pt; margin-top: 10pt; margin-bottom: 2pt; }
p { margin-top: 2pt; margin-bottom: 4pt; }
.meta { color: #444; font-size: 10pt; }
.student { margin-top: 8pt; margin-bottom: 8pt; }
.space { border: 1px solid #bbb; margin-top: 4pt; margin-bottom: 12pt; }
.math { font-family: monospace; }
.label { font-weight: bold; margin-top: 6pt; }
.sources { color: #555; font-size: 9pt; font-style: italic; }
table { border-collapse: collapse; margin-top: 2pt; margin-bottom: 4pt; }
th, td { border: 1px solid #555; padding: 2pt 5pt; font-size: 10pt; }
"""


def _html_heading(question: Question, word: str) -> str:
    points = f" ({format_points(question.points)})" if question.points is not None else ""
    return f"<h3>{word} {question.number}{html.escape(points)}</h3>"


def _answer_space(question: Question, exam: Exam) -> str:
    """Room to answer, taller for a question worth more (between 90 and 260 points of height)."""
    share = (question.points or 0) / exam.total_points if exam.total_points else 0
    height = int(min(260, max(90, 90 + share * 700)))
    return f'<div class="space" style="height: {height}px"></div>'


def render_exam_html(exam: Exam) -> str:
    title = html.escape(exam.title)
    parts = [f"<h1>Ejercicios y examen: {title}</h1>"]
    if exam.exercises:
        parts.append("<h2>Ejercicios</h2>")
        for question in exam.exercises:
            parts.append(_html_heading(question, "Ejercicio"))
            parts.append(f'<p class="meta">Dificultad: {question.difficulty}</p>')
            parts.append(text_html(question.statement))
    parts.append("<h2>Examen de práctica</h2>")
    parts.append(f'<p class="meta">{html.escape(_exam_header(exam))}</p>')
    parts.append(
        '<p class="student">Nombre: ______________________________________ '
        "Fecha: ______________</p>"
    )
    if exam.instructions:
        parts.append(text_html(exam.instructions))
    for question in exam.questions:
        parts.append(_html_heading(question, "Pregunta"))
        parts.append(text_html(question.statement))
        parts.append(_answer_space(question, exam))
    return "".join(parts)


def _nowrap(text: str) -> str:
    return text.replace(" ", "&#160;")


def _rubric_html(question: Question) -> str:
    rows = "".join(
        f"<tr><td>{inline_html(c.criterion)}</td><td>{_nowrap(format_points(c.points))}</td></tr>"
        for c in question.rubric
    )
    return f"<table><tr><th>Criterio</th><th>Puntos</th></tr>{rows}</table>"


def _solution_html(question: Question, word: str, sections: dict[str, NoteSection]) -> str:
    parts = [_html_heading(question, word), '<p class="label">Solución</p>']
    parts.append(text_html(question.solution))
    if question.rubric:
        parts += ['<p class="label">Criterios de corrección</p>', _rubric_html(question)]
    titles = _section_titles(question, sections)
    if titles:
        parts.append(f'<p class="sources">Apuntes: {html.escape("; ".join(titles))}</p>')
    return "".join(parts)


def render_solutions_html(exam: Exam, sections: dict[str, NoteSection]) -> str:
    parts = [f"<h1>Soluciones: {html.escape(exam.title)}</h1>"]
    if exam.exercises:
        parts.append("<h2>Ejercicios</h2>")
        parts += [_solution_html(q, "Ejercicio", sections) for q in exam.exercises]
    parts.append("<h2>Examen de práctica</h2>")
    parts += [_solution_html(q, "Pregunta", sections) for q in exam.questions]
    return "".join(parts)


_MARGIN = 50


def render_pdf(body_html: str, *, title: str) -> bytes:
    """An A4 PDF of `body_html`, with `title` and `Página i de n` at the foot of every page."""
    buffer = io.BytesIO()
    story = pymupdf.Story(html=f"<body>{body_html}</body>", user_css=_CSS)
    writer = pymupdf.DocumentWriter(buffer)
    page_rect = pymupdf.paper_rect("a4")
    where = page_rect + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
    more = True
    while more:
        device = writer.begin_page(page_rect)
        more, _filled = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    document = pymupdf.open("pdf", buffer.getvalue())
    try:
        count = document.page_count
        for index, page in enumerate(document, start=1):
            page.insert_text(
                (_MARGIN, page_rect.height - _MARGIN / 2),
                f"{title} · Página {index} de {count}",
                fontsize=8,
                fontname="helv",
                color=(0.35, 0.35, 0.35),
            )
        document.set_metadata({"title": title, "creator": "Student Assistant"})
        return document.tobytes(garbage=3, deflate=True)
    finally:
        document.close()


def render_files(exam: Exam, sections: dict[str, NoteSection]) -> dict[str, str | bytes]:
    """The four files of the artifact (CPU-bound: run it in a worker thread)."""
    return {
        EXAM_MD: render_exam_markdown(exam),
        SOLUTIONS_MD: render_solutions_markdown(exam, sections),
        EXAM_PDF: render_pdf(render_exam_html(exam), title=f"Examen: {exam.title}"),
        SOLUTIONS_PDF: render_pdf(
            render_solutions_html(exam, sections), title=f"Soluciones: {exam.title}"
        ),
    }


def _request_text(options: ExamOptions) -> str:
    exercises = (
        f"Haz {options.exercises} ejercicios de práctica"
        if options.exercises
        else "No hagas ejercicios de práctica (la lista `exercises` vacía)"
    )
    return (
        f"{exercises} y un examen de {options.questions} preguntas para "
        f"{options.duration_minutes} minutos, que sume {format_points(options.total_points)}."
    )


@register
class ExamGenerator(Generator):
    kind = KIND
    title = "Ejercicios y examen"
    description = (
        "Ejercicios de práctica y un examen de prueba imprimibles (PDF), con las soluciones y "
        "los criterios de corrección aparte."
    )
    version = 1
    options_model = ExamOptions

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        options = context.options
        assert isinstance(options, ExamOptions)
        prompt = load_prompt(PROMPT_NAME)
        result = await context.structured(
            [
                {
                    "role": "user",
                    "content": [
                        context.notes_block(),
                        {"type": "text", "text": _request_text(options)},
                    ],
                }
            ],
            DraftExam,
            tool_name=TOOL_NAME,
            tool_description="Record the practice exercises and the mock exam made from the notes.",
            system=prompt.content,
            prompt_hash=prompt.hash,
        )
        draft = result.value
        warnings: list[str] = []
        exercises, questions = draft.exercises, draft.exam
        if len(exercises) > options.exercises:
            warnings.append(
                f"Claude ha propuesto {len(exercises)} ejercicios; "
                f"se guardan los {options.exercises} primeros."
            )
            exercises = exercises[: options.exercises]
        if len(questions) > options.questions:
            warnings.append(
                f"Claude ha propuesto {len(questions)} preguntas de examen; "
                f"se guardan las {options.questions} primeras."
            )
            questions = questions[: options.questions]
        if not questions:
            warnings.append("Claude no ha propuesto ninguna pregunta: el examen está vacío.")
        exam = Exam(
            title=context.topic_title,
            instructions=draft.instructions.strip(),
            duration_minutes=options.duration_minutes,
            total_points=options.total_points,
            exercises=_questions(exercises, "e", scored=False),
            questions=_questions(questions, "p", scored=True),
        )
        warnings += point_warnings(exam)
        sections = {section.anchor: section for section in context.sections}
        files = await asyncio.to_thread(render_files, exam, sections)
        return GeneratorOutput(
            files=files,
            items=[
                ItemProvenance(item=question.id, anchors=question.anchors)
                for question in [*exam.exercises, *exam.questions]
            ],
            item_texts={
                question.id: question.solution for question in [*exam.exercises, *exam.questions]
            },
            warnings=warnings,
            model=result.responses[-1].model,
            prompt_hash=prompt.hash,
        )
