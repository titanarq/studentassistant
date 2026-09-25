"""The quiz generator (`quiz`) and taking the quiz: grading and the results log.

`QuizGenerator` asks Claude (prompt `generator_quiz`) for `size` questions of the requested
`difficulty` and `types` over the topic's notes, checks them (a multiple choice whose answer is not
one of its options, a true/false that is neither, an empty question is dropped with a Spanish
warning) and writes `generated/quiz.yaml` (`Quiz`): every question with its `type`, `difficulty`,
`question`, `options`, `answer`, `explanation` and `anchors` (the note sections it comes from; its
provenance, one item per question).

Taking it: the web reads the quiz (`read_quiz`) with its manifest, the student answers, and
`record_quiz_result` grades the attempt -- a multiple choice or true/false by the chosen option, a
short answer by comparing it with the expected one (case, accents, spaces and final punctuation
aside), else by the student's own assessment after seeing the answer -- and appends a `QuizResult`
to `study/quiz-results.jsonl` through the vault, committing it. `quiz_results` reads that history.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.generators.base import (
    Generator,
    GeneratorContext,
    GeneratorOutput,
    ItemProvenance,
)
from studentassistant.generators.registry import default_registry, register
from studentassistant.generators.run import GenerationError, artifact_status, read_artifact_meta
from studentassistant.llm import StructuredOutputError, load_prompt
from studentassistant.vault import GitSync, Vault, read_generated
from studentassistant.vault.study import append_study_record, read_study_records

KIND = "quiz"
FILE_NAME = "quiz.yaml"
RESULTS_LOG = "quiz-results"
PROMPT_NAME = "generator_quiz"
TOOL_NAME = "record_quiz"

QuestionType = Literal["multiple_choice", "true_false", "short_answer"]
Difficulty = Literal["easy", "medium", "hard"]
QUESTION_TYPES: tuple[QuestionType, ...] = ("multiple_choice", "true_false", "short_answer")
TRUE, FALSE = "Verdadero", "Falso"
DIFFICULTY_NAMES = {"easy": "fácil", "medium": "media", "hard": "difícil", "mixed": "variada"}
TYPE_NAMES = {
    "multiple_choice": "opción múltiple",
    "true_false": "verdadero o falso",
    "short_answer": "respuesta corta",
}

Clock = Callable[[], datetime]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- the quiz --------------------------------------------------------------------------------------


class QuizOptions(_Strict):
    """What the student can ask for."""

    size: int = Field(default=10, ge=1, le=30, description="Number of questions.")
    difficulty: Difficulty | Literal["mixed"] = "mixed"
    types: list[QuestionType] = Field(default_factory=lambda: list(QUESTION_TYPES), min_length=1)


class DraftQuestion(BaseModel):
    """One question as Claude proposes it (checked before it is kept)."""

    type: QuestionType
    difficulty: Difficulty
    question: str
    options: list[str] = Field(
        default_factory=list, description="multiple_choice only: 3 to 5 options, one correct."
    )
    answer: str = Field(
        description="multiple_choice: the correct option's exact text; true_false: "
        "'Verdadero' or 'Falso'; short_answer: the expected short answer."
    )
    explanation: str
    anchors: list[str] = Field(description="Anchors (no '#') of the note sections it comes from.")


class QuizDraft(BaseModel):
    questions: list[DraftQuestion]


class QuizQuestion(_Strict):
    """One question of `quiz.yaml`."""

    id: str = Field(description="`q1`, `q2`...: the item id of its provenance.")
    type: QuestionType
    difficulty: Difficulty
    question: str
    options: list[str] = Field(
        default_factory=list, description="The choices; `Verdadero`/`Falso` for a true/false."
    )
    answer: str
    explanation: str = ""
    anchors: list[str] = Field(default_factory=list)


class Quiz(_Strict):
    """`generated/quiz.yaml`."""

    title: str
    difficulty: Difficulty | Literal["mixed"]
    questions: list[QuizQuestion]


def normalize_answer(text: str) -> str:
    """`text` for comparison: no accents, case, extra spaces nor surrounding punctuation."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = re.sub(r"\s+", " ", plain.casefold()).strip()
    return plain.strip(" .,;:¡!¿?\"'«»()")


def _true_false(answer: str) -> str | None:
    value = normalize_answer(answer)
    if value in {"verdadero", "v", "true", "cierto", "si"}:
        return TRUE
    if value in {"falso", "f", "false", "no"}:
        return FALSE
    return None


def _check(draft: DraftQuestion, number: int) -> tuple[QuizQuestion | None, str | None]:
    """The stored question of `draft`, or why it was dropped (Spanish)."""
    question = draft.question.strip()
    label = f"La pregunta {number}"
    if not question or not draft.answer.strip():
        return None, f"{label} venía vacía y se ha descartado."
    options: list[str] = []
    answer = draft.answer.strip()
    if draft.type == "multiple_choice":
        for option in (option.strip() for option in draft.options):
            if option and normalize_answer(option) not in map(normalize_answer, options):
                options.append(option)
        matches = [o for o in options if normalize_answer(o) == normalize_answer(answer)]
        if len(options) < 2 or not matches:
            return None, f"{label} de opción múltiple no tenía la respuesta entre sus opciones."
        answer = matches[0]
    elif draft.type == "true_false":
        value = _true_false(answer)
        if value is None:
            return None, f"{label} de verdadero o falso no decía si era verdadera o falsa."
        options, answer = [TRUE, FALSE], value
    anchors = [anchor.strip().lstrip("#") for anchor in draft.anchors if anchor.strip("# ")]
    return (
        QuizQuestion(
            id="",
            type=draft.type,
            difficulty=draft.difficulty,
            question=question,
            options=options,
            answer=answer,
            explanation=draft.explanation.strip(),
            anchors=list(dict.fromkeys(anchors)),
        ),
        None,
    )


def _request(options: QuizOptions) -> str:
    types = ", ".join(f"`{kind}` ({TYPE_NAMES[kind]})" for kind in options.types)
    return (
        f"Prepara un quiz de {options.size} preguntas.\n"
        f"Dificultad: `{options.difficulty}` ({DIFFICULTY_NAMES[options.difficulty]}).\n"
        f"Tipos de pregunta: {types}."
    )


def dump_quiz(quiz: Quiz) -> str:
    return yaml.safe_dump(
        quiz.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=100
    )


@register
class QuizGenerator(Generator):
    kind = KIND
    title = "Quiz"
    description = "Preguntas de opción múltiple, verdadero o falso y respuesta corta."
    version = 1
    options_model = QuizOptions

    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        options = context.options
        assert isinstance(options, QuizOptions)
        prompt = load_prompt(PROMPT_NAME)
        result = await context.structured(
            [
                {
                    "role": "user",
                    "content": [context.notes_block(), {"type": "text", "text": _request(options)}],
                }
            ],
            QuizDraft,
            tool_name=TOOL_NAME,
            tool_description="Record the quiz questions.",
            system=prompt.content,
            prompt_hash=prompt.hash,
        )
        warnings: list[str] = []
        questions: list[QuizQuestion] = []
        skipped_types = 0
        for number, draft in enumerate(result.value.questions, start=1):
            if draft.type not in options.types:
                skipped_types += 1
                continue
            question, problem = _check(draft, number)
            if problem is not None:
                warnings.append(problem)
            if question is not None:
                questions.append(question)
        if skipped_types:
            warnings.append(f"Se han descartado {skipped_types} preguntas de un tipo no pedido.")
        if len(questions) > options.size:
            questions = questions[: options.size]
        if not questions:
            raise StructuredOutputError(TOOL_NAME, "no usable quiz question")
        if len(questions) < options.size:
            warnings.append(
                f"El quiz tiene {len(questions)} preguntas válidas de las {options.size} pedidas."
            )
        questions = [
            question.model_copy(update={"id": f"q{number}"})
            for number, question in enumerate(questions, start=1)
        ]
        quiz = Quiz(
            title=f"Quiz: {context.topic_title}",
            difficulty=options.difficulty,
            questions=questions,
        )
        return GeneratorOutput(
            files={FILE_NAME: dump_quiz(quiz)},
            items=[ItemProvenance(item=q.id, anchors=q.anchors) for q in questions],
            warnings=warnings,
            model=result.responses[-1].model,
            prompt_hash=prompt.hash,
        )


# -- taking it -------------------------------------------------------------------------------------


class QuizNotFoundError(GenerationError):
    def __init__(self) -> None:
        super().__init__("Todavía no hay quiz de este tema: genéralo primero.")


class QuizChangedError(GenerationError):
    def __init__(self) -> None:
        super().__init__(
            "El quiz ha cambiado mientras lo hacías (se ha vuelto a generar): empieza el nuevo."
        )


class InvalidAttemptError(GenerationError):
    pass


class QuizAnswer(_Strict):
    """The student's answer to one question."""

    question: str = Field(description="The question's id.")
    given: str | None = Field(default=None, description="The option chosen or the text written.")
    self_assessed: bool | None = Field(
        default=None,
        description="Short answer only: the student's own verdict after seeing the answer.",
    )


class QuizAttempt(_Strict):
    """One attempt at the quiz, as the web sends it."""

    built_at: datetime = Field(description="`built_at` of the quiz manifest answered.")
    answers: list[QuizAnswer]
    duration_seconds: int | None = Field(default=None, ge=0)


class GradedAnswer(_Strict):
    question: str
    type: QuestionType
    difficulty: Difficulty
    given: str | None
    expected: str
    correct: bool
    graded_by: Literal["auto", "student"]
    anchors: list[str] = Field(default_factory=list)


class QuizResult(_Strict):
    """One line of `study/quiz-results.jsonl`."""

    time: datetime
    quiz_built_at: datetime
    generator_version: int
    notes_version: int | None = None
    notes_sha256: str
    total: int
    correct: int
    duration_seconds: int | None = None
    answers: list[GradedAnswer]


class StoredQuiz(_Strict):
    """A topic's quiz with what the web shows about it."""

    quiz: Quiz
    built_at: datetime
    notes_version: int | None = None
    warnings: list[str] = Field(default_factory=list)
    stale: bool = False
    stale_reason: str | None = Field(default=None, description="Spanish, when `stale`.")


def read_quiz(vault: Vault, subject: str, topic: str) -> StoredQuiz | None:
    """The topic's `quiz.yaml` and its manifest (stale or not), or `None` when there is no quiz.

    Raises:
        GenerationError: the quiz or its manifest cannot be read.
        VaultError: the topic cannot be read.
    """
    meta = read_artifact_meta(vault, subject, topic, KIND)
    data = read_generated(vault, subject, topic, FILE_NAME)
    if meta is None or data is None:
        return None
    try:
        quiz = Quiz.model_validate(yaml.safe_load(data.decode("utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise GenerationError(f"No se puede leer el quiz: {error}") from error
    status = artifact_status(vault, subject, topic, KIND, registry=default_registry)
    return StoredQuiz(
        quiz=quiz,
        built_at=meta.built_at,
        notes_version=meta.notes.version,
        warnings=meta.warnings,
        stale=status.stale,
        stale_reason=status.stale_reason,
    )


def grade(question: QuizQuestion, answer: QuizAnswer | None) -> GradedAnswer:
    """`answer` to `question` graded; no answer (or an empty one) is wrong."""
    given = answer.given.strip() if answer is not None and answer.given else None
    graded_by: Literal["auto", "student"] = "auto"
    correct = given is not None and normalize_answer(given) == normalize_answer(question.answer)
    if (
        given is not None
        and not correct
        and question.type == "short_answer"
        and answer is not None
        and answer.self_assessed is not None
    ):
        correct, graded_by = answer.self_assessed, "student"
    return GradedAnswer(
        question=question.id,
        type=question.type,
        difficulty=question.difficulty,
        given=given,
        expected=question.answer,
        correct=correct,
        graded_by=graded_by,
        anchors=question.anchors,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def record_quiz_result(
    vault: Vault,
    subject: str,
    topic: str,
    attempt: QuizAttempt,
    *,
    sync: GitSync,
    clock: Clock = _utc_now,
) -> QuizResult:
    """Grade `attempt` against the topic's current quiz, append it to the results and commit.

    Raises:
        QuizNotFoundError: the topic has no quiz.
        QuizChangedError: the quiz was generated again after the attempt started.
        InvalidAttemptError: an answer names a question the quiz lacks, or one twice.
        GenerationError, VaultError: the quiz or the topic cannot be read or written.
    """
    stored = read_quiz(vault, subject, topic)
    if stored is None:
        raise QuizNotFoundError()
    if stored.built_at != attempt.built_at:
        raise QuizChangedError()
    meta = read_artifact_meta(vault, subject, topic, KIND)
    assert meta is not None  # read_quiz found it
    ids = {question.id for question in stored.quiz.questions}
    answers: dict[str, QuizAnswer] = {}
    for answer in attempt.answers:
        if answer.question not in ids:
            raise InvalidAttemptError(f"El quiz no tiene la pregunta «{answer.question}».")
        if answer.question in answers:
            raise InvalidAttemptError(f"La pregunta «{answer.question}» está respondida dos veces.")
        answers[answer.question] = answer
    graded = [grade(question, answers.get(question.id)) for question in stored.quiz.questions]
    result = QuizResult(
        time=clock(),
        quiz_built_at=stored.built_at,
        generator_version=meta.generator_version,
        notes_version=meta.notes.version,
        notes_sha256=meta.notes.sha256,
        total=len(graded),
        correct=sum(answer.correct for answer in graded),
        duration_seconds=attempt.duration_seconds,
        answers=graded,
    )
    append_study_record(vault, subject, topic, RESULTS_LOG, result)
    sync.checkpoint(f"Resultado del quiz de {subject}/{topic}: {result.correct}/{result.total}")
    return result


def quiz_results(vault: Vault, subject: str, topic: str) -> list[QuizResult]:
    """Every recorded attempt at the topic's quiz, oldest first."""
    return read_study_records(vault, subject, topic, RESULTS_LOG, QuizResult)
