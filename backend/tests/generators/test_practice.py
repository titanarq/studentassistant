"""Spaced-repetition practice over a topic's flashcards and quiz questions (#81)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from quiz_replies import MULTIPLE_CHOICE, SHORT_ANSWER, TRUE_FALSE, reply_quiz
from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.config import VaultGitSettings
from studentassistant.generators import GeneratorRegistry, run_generator
from studentassistant.generators.flashcards import TOOL_NAME as FLASHCARDS_TOOL
from studentassistant.generators.flashcards import FlashcardsGenerator
from studentassistant.generators.practice import (
    MAX_INTERVAL_DAYS,
    MIN_EASE,
    RELEARN_DELAY,
    InvalidReviewError,
    ItemState,
    PracticeAnswer,
    PracticeItemNotFoundError,
    PracticeReview,
    practice_history,
    practice_queue,
    quiz_item_key,
    record_practice_review,
    replay,
    schedule,
)
from studentassistant.generators.quiz import QuizGenerator
from studentassistant.llm import FakeClaude, LedgerBinding
from studentassistant.vault import GitSync, Vault, write_notes
from studentassistant.vault.study import study_log_path

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
DAY = timedelta(days=1)

CARDS: list[dict[str, Any]] = [
    {"front": "¿Qué es la derivada?", "back": "Un límite.", "anchors": ["definicion"]},
    {"front": "¿Qué se verá el próximo día?", "back": "La regla de la cadena.", "anchors": []},
]


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


# -- the scheduler ---------------------------------------------------------------------------------


def test_good_reviews_grow_the_interval() -> None:
    state = ItemState()
    intervals = []
    moment = NOW
    for _ in range(4):
        state = schedule(state, "good", moment)
        intervals.append(state.interval_days)
        assert state.due == moment + timedelta(days=state.interval_days)
        moment = state.due
    assert intervals == [1.0, 6.0, 15.0, 37.5]
    assert (state.reviews, state.repetitions, state.lapses) == (4, 4, 0)
    assert state.first_review == NOW and state.last_rating == "good"


def test_again_relearns_soon_and_counts_a_lapse_only_once_learned() -> None:
    first = schedule(ItemState(), "again", NOW)
    assert first.due == NOW + RELEARN_DELAY and first.lapses == 0 and first.repetitions == 0
    learned = schedule(schedule(first, "good", NOW), "good", NOW + DAY)
    forgotten = schedule(learned, "again", NOW + 7 * DAY)
    assert forgotten.lapses == 1 and forgotten.repetitions == 0 and forgotten.interval_days == 0
    assert forgotten.ease == pytest.approx(learned.ease - 0.2)
    assert schedule(forgotten, "good", NOW + 8 * DAY).interval_days == 1.0


def test_hard_and_easy() -> None:
    learned = schedule(schedule(ItemState(), "good", NOW), "good", NOW + DAY)
    hard = schedule(learned, "hard", NOW + 7 * DAY)
    assert hard.interval_days == pytest.approx(6 * 1.2) and hard.ease == pytest.approx(2.35)
    easy = schedule(learned, "easy", NOW + 7 * DAY)
    assert easy.interval_days == pytest.approx(6 * 2.5 * 1.3) and easy.ease == pytest.approx(2.65)
    assert schedule(ItemState(), "easy", NOW).interval_days == 4.0
    assert schedule(ItemState(), "hard", NOW).interval_days == 1.0


def test_ease_floor_and_interval_cap() -> None:
    state = ItemState()
    for _ in range(20):
        state = schedule(state, "again", NOW)
    assert state.ease == MIN_EASE
    long = ItemState(repetitions=5, interval_days=300.0, ease=2.5)
    assert schedule(long, "good", NOW).interval_days == MAX_INTERVAL_DAYS


def test_replay_orders_by_time_so_merged_logs_agree() -> None:
    reviews = [
        PracticeReview(time=NOW + DAY, item="flashcards:c1", source="flashcards", rating="good"),
        PracticeReview(time=NOW, item="flashcards:c1", source="flashcards", rating="again"),
    ]
    assert replay(reviews) == replay(list(reversed(reviews)))
    state = replay(reviews)["flashcards:c1"]
    assert state.first_review == NOW and state.last_rating == "good"


# -- the vault -------------------------------------------------------------------------------------


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


class ManualClock:
    """Monotonic seconds that only move when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def _git(topic: ReviseTopic, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(topic.vault.path), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def _generate(topic: ReviseTopic, kind: str, fake: FakeClaude, **options: object) -> None:
    registry = GeneratorRegistry()
    registry.register(QuizGenerator)
    registry.register(FlashcardsGenerator)
    client = fake.client("generator", ledger=LedgerBinding(topic.vault, topic.subject, topic.topic))
    _run(
        run_generator(
            topic.vault,
            topic.subject,
            topic.topic,
            kind,
            client=client,
            sync=GitSync(topic.vault),
            registry=registry,
            options=options,
        )
    )


def _with_cards(topic: ReviseTopic) -> None:
    fake = FakeClaude()
    fake.reply_tool(FLASHCARDS_TOOL, {"cards": CARDS})
    _generate(topic, "flashcards", fake)


def _with_quiz(topic: ReviseTopic, *questions: dict[str, Any]) -> None:
    fake = FakeClaude()
    reply_quiz(fake, *questions)
    _generate(topic, "quiz", fake, size=len(questions) or 3)


@pytest.fixture
def material(topic: ReviseTopic) -> ReviseTopic:
    _with_cards(topic)
    _with_quiz(topic)
    return topic


def _queue(topic: ReviseTopic, now: datetime = NOW, **kwargs: Any):
    return practice_queue(topic.vault, topic.subject, topic.topic, now=now, tz=UTC, **kwargs)


def _review(topic: ReviseTopic, sync: GitSync, when: datetime = NOW, **answer: Any):
    return record_practice_review(
        topic.vault,
        topic.subject,
        topic.topic,
        PracticeAnswer(**answer),
        sync=sync,
        clock=lambda: when,
    )


def test_no_material(topic: ReviseTopic) -> None:
    queue = _queue(topic)
    assert queue.queue == [] and queue.counts.total == 0
    assert "Todavía no hay flashcards ni quiz" in queue.warnings[0]


def test_queue_offers_cards_then_questions_as_new_items(material: ReviseTopic) -> None:
    queue = _queue(material)
    keys = [queued.item.key for queued in queue.queue]
    assert len(keys) == 5 and keys[0].startswith("flashcards:c") and keys[2].startswith("quiz:")
    assert keys[2] == quiz_item_key(MULTIPLE_CHOICE["question"])
    question = queue.queue[2].item
    assert question.question_type == "multiple_choice" and question.answer == "Un límite"
    assert question.options == ["Un límite", "Una integral", "Una suma"]
    assert queue.queue[0].item.prompt == CARDS[0]["front"]
    assert queue.counts.model_dump() == {
        "total": 5,
        "due": 0,
        "new": 5,
        "unseen": 5,
        "learned": 0,
        "new_today": 0,
    }
    assert queue.next_due is None and queue.warnings == []
    assert len(_queue(material, new_limit=2).queue) == 2


def test_flashcard_review_is_stored_and_scheduled(material: ReviseTopic, sync: GitSync) -> None:
    card = _queue(material).queue[0].item
    outcome = _review(material, sync, item=card.key, rating="good")
    assert outcome.review.rating == "good" and outcome.review.source == "flashcards"
    assert outcome.state.due == NOW + DAY
    assert practice_history(material.vault, material.subject, material.topic) == [outcome.review]
    path = study_log_path(material.vault, material.subject, material.topic, "practice")
    assert path.name == "practice.jsonl" and path.parent.name == "study"
    assert sync.status().pending_changes  # committed with the sitting's batch, not alone

    later = _queue(material, NOW + timedelta(hours=1))
    assert card.key not in [queued.item.key for queued in later.queue]
    assert later.counts.learned == 1 and later.counts.new_today == 1 and later.counts.new == 4
    assert later.next_due == NOW + DAY
    tomorrow = _queue(material, NOW + DAY)
    assert tomorrow.queue[0].item.key == card.key and tomorrow.counts.due == 1
    assert tomorrow.queue[0].state is not None and tomorrow.counts.new_today == 0


def test_the_reviews_of_a_sitting_share_one_commit(material: ReviseTopic) -> None:
    clock = ManualClock()
    settings = VaultGitSettings(commit_quiet_seconds=5, commit_max_delay_seconds=60)
    sync = GitSync(material.vault, settings, clock)
    sync.checkpoint("Generación")  # the generator's last conversation record waits for a batch
    head = _git(material, "rev-parse", "HEAD").strip()
    keys = [queued.item.key for queued in _queue(material).queue[:2]]
    for minutes, key in enumerate(keys):
        _review(material, sync, NOW + timedelta(minutes=minutes), item=key, rating="good")
        clock.now += 3
    right = quiz_item_key(MULTIPLE_CHOICE["question"])
    _review(material, sync, NOW + timedelta(minutes=2), item=right, given="Un límite")
    path = study_log_path(material.vault, material.subject, material.topic, "practice")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3

    clock.now += 4
    sync.run_due()  # still inside the quiet window
    assert _git(material, "rev-parse", "HEAD").strip() == head and sync.status().pending_changes

    clock.now += 1
    sync.run_due()
    assert _git(material, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    assert _git(material, "log", "-1", "--format=%s").strip() == "1 archivo cambiado"
    relative = path.relative_to(material.vault.path).as_posix()
    assert _git(material, "show", "--format=", "--name-only", "HEAD").split() == [relative]
    committed = _git(material, "show", f"HEAD:{relative}")
    assert len(committed.splitlines()) == 3 and all(key in committed for key in [*keys, right])
    assert not sync.status().pending_changes and _git(material, "status", "--porcelain") == ""


def test_daily_new_limit_counts_what_was_started_today(
    material: ReviseTopic, sync: GitSync
) -> None:
    first = _queue(material, new_limit=2).queue[0].item
    _review(material, sync, item=first.key, rating="easy")
    queue = _queue(material, NOW + timedelta(minutes=5), new_limit=2)
    assert queue.counts.new == 1 and queue.counts.new_today == 1


def test_again_brings_the_item_back_in_the_session(material: ReviseTopic, sync: GitSync) -> None:
    card = _queue(material).queue[1].item
    outcome = _review(material, sync, item=card.key, rating="again")
    assert outcome.state.due == NOW + RELEARN_DELAY
    assert _queue(material, NOW + RELEARN_DELAY).queue[0].item.key == card.key


def test_quiz_answers_are_graded(material: ReviseTopic, sync: GitSync) -> None:
    right = quiz_item_key(MULTIPLE_CHOICE["question"])
    outcome = _review(material, sync, item=right, given="Un límite", rating="again")
    assert (outcome.review.correct, outcome.review.rating) == (True, "good")
    assert outcome.review.graded_by == "auto" and outcome.review.anchors == ["definicion"]

    easy = _review(material, sync, item=right, given="un limite", rating="easy")
    assert easy.review.rating == "easy" and easy.state.repetitions == 2

    wrong = _review(
        material, sync, item=quiz_item_key(TRUE_FALSE["question"]), given="Falso", rating="easy"
    )
    assert (wrong.review.correct, wrong.review.rating) == (False, "again")

    short = quiz_item_key(SHORT_ANSWER["question"])
    judged = _review(material, sync, item=short, given="la cadena", self_assessed=True)
    assert judged.review.correct and judged.review.graded_by == "student"
    blank = _review(material, sync, item=short)
    assert blank.review.correct is False and blank.review.rating == "again"


def test_refusals(material: ReviseTopic, sync: GitSync) -> None:
    with pytest.raises(PracticeItemNotFoundError, match="quiz:nada"):
        _review(material, sync, item="quiz:nada", rating="good")
    card = _queue(material).queue[0].item
    with pytest.raises(InvalidReviewError, match="otra vez"):
        _review(material, sync, item=card.key)
    assert practice_history(material.vault, material.subject, material.topic) == []


def test_a_regenerated_quiz_keeps_the_history_of_the_same_question(
    material: ReviseTopic, sync: GitSync
) -> None:
    key = quiz_item_key(SHORT_ANSWER["question"])
    _review(material, sync, item=key, given="La regla de la cadena")
    _with_quiz(material, SHORT_ANSWER, MULTIPLE_CHOICE)  # reordered: now q1
    queue = _queue(material, NOW + timedelta(hours=1))
    assert key not in [queued.item.key for queued in queue.queue]
    assert queue.counts.learned == 1 and queue.counts.total == 4
    assert _queue(material, NOW + DAY).queue[0].item.key == key


def test_stale_material_is_warned(material: ReviseTopic) -> None:
    write_notes(material.vault, material.subject, material.topic, "# Derivadas\n\nOtra cosa.\n")
    warnings = _queue(material).warnings
    assert any("flashcards" in warning for warning in warnings)
    assert any("quiz" in warning for warning in warnings)
