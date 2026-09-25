"""Correcting the mock exam: the student grades their paper answers with the rubric.

The student sits the exam on paper (`examen.pdf`), then in the web reads each question's worked
solution and rubric (`read_exam`, from `generated/examen.yaml` and its manifest) and gives
themselves the points of every criterion. `record_exam_result` checks the correction against the
current exam -- every criterion between 0 and its points, known questions only, the exam not
generated again since the student opened it -- computes each question's score, the total out of
the exam's points and the percentage, appends an `ExamResult` to `study/exam-results.jsonl`
through the vault and commits it. `exam_results` reads that history.

A question without a rubric is graded as a whole (one "criterion" worth its points); a question
Claude did not score is worth what its rubric adds up to. A question the correction leaves out
scores 0. An exam generated before `examen.yaml` existed (generator version 1) cannot be corrected:
it reads as no exam, and generating it again fixes that.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.generators.exam import EXAM_YAML, KIND, ExamFile, ExamQuestion
from studentassistant.generators.registry import default_registry
from studentassistant.generators.run import GenerationError, artifact_status, read_artifact_meta
from studentassistant.vault import GitSync, Vault, read_generated
from studentassistant.vault.study import append_study_record, read_study_records

RESULTS_LOG = "exam-results"
WHOLE_QUESTION = "Pregunta completa"

_TOLERANCE = 1e-6

Clock = Callable[[], datetime]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExamNotFoundError(GenerationError):
    def __init__(self) -> None:
        super().__init__(
            "Todavía no hay examen que corregir en este tema: genera el examen "
            "(o vuelve a generarlo si es de antes de poder corregirlo)."
        )


class ExamChangedError(GenerationError):
    def __init__(self) -> None:
        super().__init__(
            "El examen ha cambiado mientras lo corregías (se ha vuelto a generar): "
            "corrige el nuevo."
        )


class InvalidCorrectionError(GenerationError):
    pass


class StoredExam(_Strict):
    """A topic's exam with what the web shows about it."""

    exam: ExamFile
    built_at: datetime
    notes_version: int | None = None
    warnings: list[str] = Field(default_factory=list)
    stale: bool = False
    stale_reason: str | None = Field(default=None, description="Spanish, when `stale`.")


class QuestionCorrection(_Strict):
    """The points the student gives themselves for one question."""

    question: str = Field(description="The question's id (`p1`, `p2`...).")
    awarded: list[float] = Field(
        description="Points per rubric criterion, in the rubric's order; one value for a "
        "question without rubric."
    )


class ExamAttempt(_Strict):
    """A correction of the exam, as the web sends it."""

    built_at: datetime = Field(description="`built_at` of the exam manifest corrected.")
    questions: list[QuestionCorrection]


class AwardedCriterion(_Strict):
    criterion: str
    points: float
    awarded: float


class QuestionScore(_Strict):
    question: str
    number: int
    points: float = Field(description="What the question is worth.")
    score: float
    criteria: list[AwardedCriterion]
    anchors: list[str] = Field(default_factory=list)


class ExamResult(_Strict):
    """One line of `study/exam-results.jsonl`."""

    time: datetime
    exam_built_at: datetime
    generator_version: int
    notes_version: int | None = None
    notes_sha256: str
    score: float
    total: float = Field(description="What the exam is worth: its questions' points added up.")
    percentage: float
    questions: list[QuestionScore]


def read_exam(vault: Vault, subject: str, topic: str) -> StoredExam | None:
    """The topic's `examen.yaml` and its manifest (stale or not), or `None` when there is none.

    Raises:
        GenerationError: the exam or its manifest cannot be read.
        VaultError: the topic cannot be read.
    """
    meta = read_artifact_meta(vault, subject, topic, KIND)
    data = read_generated(vault, subject, topic, EXAM_YAML)
    if meta is None or data is None:
        return None
    try:
        exam = ExamFile.model_validate(yaml.safe_load(data.decode("utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise GenerationError(f"No se puede leer el examen: {error}") from error
    status = artifact_status(vault, subject, topic, KIND, registry=default_registry)
    return StoredExam(
        exam=exam,
        built_at=meta.built_at,
        notes_version=meta.notes.version,
        warnings=meta.warnings,
        stale=status.stale,
        stale_reason=status.stale_reason,
    )


def question_points(question: ExamQuestion) -> float:
    """What `question` is worth: its points, else what its rubric adds up to."""
    if question.points is not None:
        return question.points
    return sum(criterion.points for criterion in question.rubric)


def criteria(question: ExamQuestion) -> list[tuple[str, float]]:
    """The criteria the student grades `question` by: its rubric, else the whole question."""
    if question.rubric:
        return [(criterion.criterion, criterion.points) for criterion in question.rubric]
    return [(WHOLE_QUESTION, question_points(question))]


def _number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    rounded = round(value, 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}".replace(".", ",")


def score_question(question: ExamQuestion, correction: QuestionCorrection | None) -> QuestionScore:
    """`correction` of `question` checked and added up; no correction scores 0.

    Raises:
        InvalidCorrectionError: the correction has not one value per criterion, or a value out
            of its criterion's range.
    """
    wanted = criteria(question)
    awarded = correction.awarded if correction is not None else [0.0] * len(wanted)
    if len(awarded) != len(wanted):
        raise InvalidCorrectionError(
            f"La pregunta {question.number} tiene {len(wanted)} criterios y la corrección "
            f"trae {len(awarded)} puntuaciones."
        )
    scored: list[AwardedCriterion] = []
    for (criterion, points), value in zip(wanted, awarded, strict=True):
        if not math.isfinite(value) or value < 0 or value > points + _TOLERANCE:
            raise InvalidCorrectionError(
                f"En la pregunta {question.number}, «{criterion}» vale entre 0 y "
                f"{_number(points)} puntos, no {_number(value)}."
            )
        scored.append(AwardedCriterion(criterion=criterion, points=points, awarded=value))
    worth = question_points(question)
    score = min(worth, sum(item.awarded for item in scored))
    return QuestionScore(
        question=question.id,
        number=question.number,
        points=worth,
        score=round(score, 4),
        criteria=scored,
        anchors=question.anchors,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def record_exam_result(
    vault: Vault,
    subject: str,
    topic: str,
    attempt: ExamAttempt,
    *,
    sync: GitSync,
    clock: Clock = _utc_now,
) -> ExamResult:
    """Score `attempt` against the topic's current exam, append it to the results and commit.

    Raises:
        ExamNotFoundError: the topic has no exam to correct.
        ExamChangedError: the exam was generated again after the correction started.
        InvalidCorrectionError: an unknown question, one corrected twice, or points out of range.
        GenerationError, VaultError: the exam or the topic cannot be read or written.
    """
    stored = read_exam(vault, subject, topic)
    if stored is None:
        raise ExamNotFoundError()
    if stored.built_at != attempt.built_at:
        raise ExamChangedError()
    meta = read_artifact_meta(vault, subject, topic, KIND)
    assert meta is not None  # read_exam found it
    ids = {question.id for question in stored.exam.questions}
    corrections: dict[str, QuestionCorrection] = {}
    for correction in attempt.questions:
        if correction.question not in ids:
            raise InvalidCorrectionError(f"El examen no tiene la pregunta «{correction.question}».")
        if correction.question in corrections:
            raise InvalidCorrectionError(
                f"La pregunta «{correction.question}» está corregida dos veces."
            )
        corrections[correction.question] = correction
    scores = [score_question(q, corrections.get(q.id)) for q in stored.exam.questions]
    score = round(sum(item.score for item in scores), 4)
    total = round(sum(item.points for item in scores), 4)
    result = ExamResult(
        time=clock(),
        exam_built_at=stored.built_at,
        generator_version=meta.generator_version,
        notes_version=meta.notes.version,
        notes_sha256=meta.notes.sha256,
        score=score,
        total=total,
        percentage=round(100 * score / total, 1) if total > 0 else 0.0,
        questions=scores,
    )
    append_study_record(vault, subject, topic, RESULTS_LOG, result)
    sync.checkpoint(
        f"Corrección del examen de {subject}/{topic}: {_number(score)}/{_number(total)}"
    )
    return result


def exam_results(vault: Vault, subject: str, topic: str) -> list[ExamResult]:
    """Every recorded correction of the topic's exam, oldest first."""
    return read_study_records(vault, subject, topic, RESULTS_LOG, ExamResult)
