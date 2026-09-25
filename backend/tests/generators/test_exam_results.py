"""Correcting the mock exam with its rubric: reading it, scoring, bounds, history, refusals."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.generators import default_registry, run_generator
from studentassistant.generators.exam import EXAM_YAML, KIND, TOOL_NAME
from studentassistant.generators.exam_results import (
    WHOLE_QUESTION,
    ExamAttempt,
    ExamChangedError,
    ExamNotFoundError,
    InvalidCorrectionError,
    QuestionCorrection,
    exam_results,
    read_exam,
    record_exam_result,
)
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, generated_directory, write_notes
from studentassistant.vault.study import study_log_path

NOW = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)

QUESTIONS: list[dict[str, Any]] = [
    {
        "statement": "Define la derivada de f en a.",
        "difficulty": "media",
        "points": 4,
        "solution": "Es el límite del cociente incremental.",
        "rubric": [
            {"criterion": "Escribe el cociente incremental", "points": 2},
            {"criterion": "Toma el límite cuando h → 0", "points": 2},
        ],
        "anchors": ["definicion"],
    },
    {
        "statement": "¿Qué regla se verá el próximo día?",
        "difficulty": "baja",
        "points": 6,
        "solution": "La regla de la cadena.",
        "rubric": [],
        "anchors": ["proximo-dia"],
    },
]


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _generate(topic: ReviseTopic, sync: GitSync, *, built: datetime = NOW) -> None:
    fake = FakeClaude()
    fake.reply_tool(TOOL_NAME, {"instructions": "", "exercises": [], "exam": QUESTIONS})
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            KIND,
            client=client,
            sync=sync,
            registry=default_registry,
            options={"exercises": 0, "questions": 2},
            clock=lambda: built,
        )
    )


@pytest.fixture
def exam_topic(topic: ReviseTopic, sync: GitSync) -> ReviseTopic:
    _generate(topic, sync)
    return topic


def _record(topic: ReviseTopic, sync: GitSync, *corrections: QuestionCorrection, built=NOW):
    return record_exam_result(
        topic.vault,
        topic.subject,
        topic.topic,
        ExamAttempt(built_at=built, questions=list(corrections)),
        sync=sync,
        clock=lambda: NOW,
    )


def test_read_exam(exam_topic: ReviseTopic) -> None:
    topic = exam_topic
    stored = read_exam(topic.vault, topic.subject, topic.topic)
    assert stored is not None
    assert stored.built_at == NOW
    assert not stored.stale and stored.stale_reason is None
    assert [q.id for q in stored.exam.questions] == ["p1", "p2"]
    assert stored.exam.questions[0].rubric[1].criterion == "Toma el límite cuando h → 0"


def test_read_exam_is_stale_when_the_notes_change(exam_topic: ReviseTopic) -> None:
    topic = exam_topic
    write_notes(topic.vault, topic.subject, topic.topic, "# Derivadas\n\nOtra cosa.\n")
    stored = read_exam(topic.vault, topic.subject, topic.topic)
    assert stored is not None and stored.stale and stored.stale_reason


def test_no_exam_or_an_exam_without_yaml_reads_as_none(topic: ReviseTopic, sync: GitSync) -> None:
    assert read_exam(topic.vault, topic.subject, topic.topic) is None
    _generate(topic, sync)
    (generated_directory(topic.vault, topic.subject, topic.topic) / EXAM_YAML).unlink()
    assert read_exam(topic.vault, topic.subject, topic.topic) is None
    with pytest.raises(ExamNotFoundError, match="Todavía no hay examen"):
        _record(topic, sync)


def test_scores_records_and_commits(exam_topic: ReviseTopic, sync: GitSync) -> None:
    topic = exam_topic
    result = _record(
        topic,
        sync,
        QuestionCorrection(question="p1", awarded=[2, 0.5]),
        QuestionCorrection(question="p2", awarded=[3]),
    )

    assert (result.score, result.total, result.percentage) == (5.5, 10, 55.0)
    first, second = result.questions
    assert (first.question, first.points, first.score) == ("p1", 4, 2.5)
    assert [(c.criterion, c.points, c.awarded) for c in first.criteria] == [
        ("Escribe el cociente incremental", 2, 2),
        ("Toma el límite cuando h → 0", 2, 0.5),
    ]
    assert [(c.criterion, c.points) for c in second.criteria] == [(WHOLE_QUESTION, 6)]
    assert second.anchors == ["proximo-dia"]
    assert result.exam_built_at == NOW and result.generator_version == 2
    path = study_log_path(topic.vault, topic.subject, topic.topic, "exam-results")
    assert path.name == "exam-results.jsonl" and path.parent.name == "study"
    assert exam_results(topic.vault, topic.subject, topic.topic) == [result]
    last = subprocess.run(
        ["git", "-C", str(topic.vault.path), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert last.stdout.startswith("Corrección del examen de matematicas/derivadas: 5,5/10")


def test_a_question_left_out_scores_zero_and_history_grows(
    exam_topic: ReviseTopic, sync: GitSync
) -> None:
    topic = exam_topic
    first = _record(topic, sync, QuestionCorrection(question="p2", awarded=[6]))
    assert (first.score, first.percentage) == (6, 60.0)
    assert [c.awarded for c in first.questions[0].criteria] == [0, 0]
    second = _record(
        topic,
        sync,
        QuestionCorrection(question="p1", awarded=[2, 2]),
        QuestionCorrection(question="p2", awarded=[6]),
    )
    assert second.percentage == 100.0
    history = exam_results(topic.vault, topic.subject, topic.topic)
    assert [entry.score for entry in history] == [6, 10]


@pytest.mark.parametrize(
    ("correction", "message"),
    [
        (QuestionCorrection(question="p9", awarded=[1]), "no tiene la pregunta «p9»"),
        (QuestionCorrection(question="p1", awarded=[3, 0]), "vale entre 0 y 2 puntos, no 3"),
        (QuestionCorrection(question="p1", awarded=[-1, 0]), "no -1"),
        (QuestionCorrection(question="p2", awarded=[6.5]), "«Pregunta completa» vale entre 0 y 6"),
        (QuestionCorrection(question="p1", awarded=[1]), "tiene 2 criterios"),
        (QuestionCorrection(question="p1", awarded=[float("nan"), 0]), "pregunta 1"),
    ],
)
def test_corrections_out_of_range_are_refused(
    exam_topic: ReviseTopic, sync: GitSync, correction: QuestionCorrection, message: str
) -> None:
    topic = exam_topic
    with pytest.raises(InvalidCorrectionError, match=message):
        _record(topic, sync, correction)
    assert exam_results(topic.vault, topic.subject, topic.topic) == []


def test_a_question_corrected_twice_is_refused(exam_topic: ReviseTopic, sync: GitSync) -> None:
    twice = QuestionCorrection(question="p2", awarded=[1])
    with pytest.raises(InvalidCorrectionError, match="dos veces"):
        _record(exam_topic, sync, twice, twice)


def test_an_exam_regenerated_since_is_refused(exam_topic: ReviseTopic, sync: GitSync) -> None:
    topic = exam_topic
    later = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    _generate(topic, sync, built=later)
    with pytest.raises(ExamChangedError, match="ha cambiado"):
        _record(topic, sync, QuestionCorrection(question="p2", awarded=[1]))
    assert _record(topic, sync, built=later).score == 0
