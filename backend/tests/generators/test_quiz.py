"""The quiz generator, grading an attempt and the results log."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Awaitable
from datetime import UTC, datetime

import pytest
import yaml

from quiz_replies import MULTIPLE_CHOICE, SHORT_ANSWER, TRUE_FALSE, reply_quiz
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.generators import (
    GeneratorRegistry,
    InvalidOptionsError,
    default_registry,
    run_generator,
)
from studentassistant.generators.quiz import (
    InvalidAttemptError,
    QuizAnswer,
    QuizAttempt,
    QuizChangedError,
    QuizGenerator,
    QuizNotFoundError,
    normalize_answer,
    quiz_results,
    read_quiz,
    record_quiz_result,
)
from studentassistant.llm import FakeClaude, LedgerBinding, StructuredOutputError
from studentassistant.vault import GitSync, Vault, generated_directory, write_notes
from studentassistant.vault.study import study_log_path

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


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


def _registry() -> GeneratorRegistry:
    registry = GeneratorRegistry()
    registry.register(QuizGenerator)
    return registry


def _generate(topic: ReviseTopic, fake: FakeClaude, sync: GitSync, **options: object):
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    return _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            "quiz",
            client=client,
            sync=sync,
            registry=_registry(),
            options=options,
        )
    )


def test_quiz_is_registered_by_default() -> None:
    assert default_registry.lookup("quiz") is QuizGenerator


def test_generates_quiz_yaml_with_provenance(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    reply_quiz(fake)
    result = _generate(topic, fake, sync, size=3, difficulty="mixed")

    assert result.items == 3 and result.unresolved == []
    assert [(entry.item, entry.anchors) for entry in result.ungrounded] == [("q3", ["proximo-dia"])]
    assert result.warnings == [
        "1 elemento no se apoya claramente en los apuntes: q3 (50 %, #proximo-dia)."
        " Revísalos antes de estudiar con ellos."
    ]
    data = yaml.safe_load(
        (generated_directory(topic.vault, topic.subject, topic.topic) / "quiz.yaml").read_text()
    )
    assert data["title"] == "Quiz: Derivadas"
    first, second, third = data["questions"]
    assert first["id"] == "q1" and first["answer"] == "Un límite"
    assert first["anchors"] == ["definicion"]
    assert second["options"] == ["Verdadero", "Falso"] and second["answer"] == "Verdadero"
    assert third["type"] == "short_answer" and third["difficulty"] == "hard"
    stored = read_quiz(topic.vault, topic.subject, topic.topic)
    assert stored is not None and stored.notes_version == result.notes.version
    assert not stored.stale
    write_notes(topic.vault, topic.subject, topic.topic, topic.notes + "\nOtro.[^p1]\n")
    stale = read_quiz(topic.vault, topic.subject, topic.topic)
    assert stale is not None and stale.stale and stale.stale_reason
    request = fake.requests[0]
    assert request.role == "generator"
    text = json.dumps(request.messages, ensure_ascii=False)
    assert "3 preguntas" in text and "`mixed`" in text and "#definicion" in text
    assert "practice quiz" in json.dumps(request.system)


def test_drops_invalid_questions_and_trims_to_size(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    bad_choice = {**MULTIPLE_CHOICE, "answer": "Otra cosa"}
    bad_true_false = {**TRUE_FALSE, "answer": "quizá"}
    reply_quiz(fake, bad_choice, bad_true_false, SHORT_ANSWER, MULTIPLE_CHOICE, TRUE_FALSE)
    result = _generate(topic, fake, sync, size=2, types=["multiple_choice", "short_answer"])

    stored = read_quiz(topic.vault, topic.subject, topic.topic)
    assert stored is not None
    assert [q.type for q in stored.quiz.questions] == ["short_answer", "multiple_choice"]
    assert [q.id for q in stored.quiz.questions] == ["q1", "q2"]
    joined = " ".join(result.warnings)
    assert "no tenía la respuesta" in joined and "tipo no pedido" in joined


def test_reports_fewer_questions_and_unknown_anchors(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    reply_quiz(fake, {**MULTIPLE_CHOICE, "anchors": ["no-existe"]})
    result = _generate(topic, fake, sync, size=5)
    assert any("1 preguntas válidas de las 5" in w for w in result.warnings)
    assert [item.item for item in result.unresolved] == ["q1"]


def test_no_usable_question_writes_nothing(topic: ReviseTopic, sync: GitSync) -> None:
    fake = FakeClaude()
    reply_quiz(fake, {**MULTIPLE_CHOICE, "question": " "})
    with pytest.raises(StructuredOutputError):
        _generate(topic, fake, sync)
    assert read_quiz(topic.vault, topic.subject, topic.topic) is None


def test_options_are_validated(topic: ReviseTopic, sync: GitSync) -> None:
    for options in ({"size": 0}, {"difficulty": "imposible"}, {"types": []}):
        with pytest.raises(InvalidOptionsError):
            _generate(topic, FakeClaude(), sync, **options)


def test_normalize_answer() -> None:
    assert normalize_answer("  La Regla  de la CADENA. ") == "la regla de la cadena"
    assert normalize_answer("¿Límite?") == "limite"


# -- taking the quiz -------------------------------------------------------------------------------


@pytest.fixture
def quiz_topic(topic: ReviseTopic, sync: GitSync) -> ReviseTopic:
    fake = FakeClaude()
    reply_quiz(fake)
    _generate(topic, fake, sync, size=3)
    return topic


def _built_at(topic: ReviseTopic) -> datetime:
    stored = read_quiz(topic.vault, topic.subject, topic.topic)
    assert stored is not None
    return stored.built_at


def test_grades_records_and_commits(quiz_topic: ReviseTopic, sync: GitSync) -> None:
    topic = quiz_topic
    attempt = QuizAttempt(
        built_at=_built_at(topic),
        answers=[
            QuizAnswer(question="q1", given="Un límite"),
            QuizAnswer(question="q2", given="Falso"),
            QuizAnswer(question="q3", given="la cadena", self_assessed=True),
        ],
        duration_seconds=95,
    )
    result = record_quiz_result(
        topic.vault, topic.subject, topic.topic, attempt, sync=sync, clock=lambda: NOW
    )

    assert (result.total, result.correct) == (3, 2)
    assert [a.correct for a in result.answers] == [True, False, True]
    assert [a.graded_by for a in result.answers] == ["auto", "auto", "student"]
    assert result.answers[1].expected == "Verdadero"
    assert result.time == NOW and result.duration_seconds == 95
    path = study_log_path(topic.vault, topic.subject, topic.topic, "quiz-results")
    assert path.name == "quiz-results.jsonl" and path.parent.name == "study"
    assert quiz_results(topic.vault, topic.subject, topic.topic) == [result]
    last = subprocess.run(
        ["git", "-C", str(topic.vault.path), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert last.stdout.startswith("Resultado del quiz de matematicas/derivadas: 2/3")


def test_short_answer_matches_without_self_assessment(
    quiz_topic: ReviseTopic, sync: GitSync
) -> None:
    topic = quiz_topic
    attempt = QuizAttempt(
        built_at=_built_at(topic),
        answers=[QuizAnswer(question="q3", given="la regla de la cadena", self_assessed=False)],
    )
    result = record_quiz_result(topic.vault, topic.subject, topic.topic, attempt, sync=sync)
    assert result.correct == 1 and result.answers[2].graded_by == "auto"
    assert result.answers[0].given is None and not result.answers[0].correct


def test_attempt_refusals(quiz_topic: ReviseTopic, sync: GitSync, tmp_vault: Vault) -> None:
    topic = quiz_topic
    built_at = _built_at(topic)
    with pytest.raises(QuizChangedError):
        record_quiz_result(
            topic.vault,
            topic.subject,
            topic.topic,
            QuizAttempt(built_at=NOW, answers=[]),
            sync=sync,
        )
    with pytest.raises(InvalidAttemptError, match="q9"):
        record_quiz_result(
            topic.vault,
            topic.subject,
            topic.topic,
            QuizAttempt(built_at=built_at, answers=[QuizAnswer(question="q9")]),
            sync=sync,
        )
    twice = [QuizAnswer(question="q1"), QuizAnswer(question="q1")]
    with pytest.raises(InvalidAttemptError, match="dos veces"):
        record_quiz_result(
            topic.vault,
            topic.subject,
            topic.topic,
            QuizAttempt(built_at=built_at, answers=twice),
            sync=sync,
        )
    assert quiz_results(topic.vault, topic.subject, topic.topic) == []


def test_no_quiz(topic: ReviseTopic, sync: GitSync) -> None:
    with pytest.raises(QuizNotFoundError):
        record_quiz_result(
            topic.vault,
            topic.subject,
            topic.topic,
            QuizAttempt(built_at=NOW, answers=[]),
            sync=sync,
        )
    assert quiz_results(topic.vault, topic.subject, topic.topic) == []


def test_partial_attempt_grades_only_the_questions_asked(
    quiz_topic: ReviseTopic, sync: GitSync
) -> None:
    topic = quiz_topic
    attempt = QuizAttempt(
        built_at=_built_at(topic),
        questions=["q3", "q2"],
        answers=[QuizAnswer(question="q2", given="Verdadero")],
    )
    result = record_quiz_result(topic.vault, topic.subject, topic.topic, attempt, sync=sync)

    assert (result.total, result.correct) == (2, 1)
    assert result.questions == ["q2", "q3"]
    assert [a.question for a in result.answers] == ["q2", "q3"]
    assert quiz_results(topic.vault, topic.subject, topic.topic) == [result]
    last = subprocess.run(
        ["git", "-C", str(topic.vault.path), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert last.stdout.startswith("Resultado parcial del quiz de matematicas/derivadas: 1/2")


def test_full_attempt_records_no_questions(quiz_topic: ReviseTopic, sync: GitSync) -> None:
    topic = quiz_topic
    attempt = QuizAttempt(built_at=_built_at(topic), answers=[])
    result = record_quiz_result(topic.vault, topic.subject, topic.topic, attempt, sync=sync)
    assert result.total == 3 and result.questions is None
    line = json.loads(
        study_log_path(topic.vault, topic.subject, topic.topic, "quiz-results")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert line["questions"] is None


def test_partial_attempt_refusals(quiz_topic: ReviseTopic, sync: GitSync) -> None:
    topic = quiz_topic
    built_at = _built_at(topic)
    cases = [
        (["q1", "q9"], [], "q9"),
        (["q1", "q1"], [], "dos veces"),
        (["q1"], [QuizAnswer(question="q2", given="Falso")], "no está entre"),
    ]
    for questions, answers, message in cases:
        with pytest.raises(InvalidAttemptError, match=message):
            record_quiz_result(
                topic.vault,
                topic.subject,
                topic.topic,
                QuizAttempt(built_at=built_at, questions=questions, answers=answers),
                sync=sync,
            )
    with pytest.raises(QuizChangedError):
        record_quiz_result(
            topic.vault,
            topic.subject,
            topic.topic,
            QuizAttempt(built_at=NOW, questions=["q1"], answers=[]),
            sync=sync,
        )
    with pytest.raises(ValueError):
        QuizAttempt(built_at=built_at, questions=[], answers=[])
    assert quiz_results(topic.vault, topic.subject, topic.topic) == []


def test_results_recorded_before_partial_attempts_still_load(
    quiz_topic: ReviseTopic, sync: GitSync
) -> None:
    topic = quiz_topic
    attempt = QuizAttempt(built_at=_built_at(topic), answers=[])
    record_quiz_result(topic.vault, topic.subject, topic.topic, attempt, sync=sync)
    path = study_log_path(topic.vault, topic.subject, topic.topic, "quiz-results")
    old = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    del old["questions"]
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")

    [loaded] = quiz_results(topic.vault, topic.subject, topic.topic)
    assert loaded.total == 3 and loaded.questions is None
