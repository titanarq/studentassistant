"""Practice with spaced repetition over a topic's flashcards and quiz questions (#81).

The practice items of a topic are its current flashcards (`generated/flashcards.yaml`) and its
current quiz questions (`generated/quiz.yaml`), each under a stable key: `flashcards:<card id>`
(card ids survive a regeneration) and `quiz:<hash of the normalized question>` (a regenerated quiz
that asks the same question keeps its history). An item that leaves the material is no longer
offered; its history stays.

Every review is one `PracticeReview` appended to `study/practice.jsonl` of the topic through
`vault.study` and committed. The schedule is never stored: `replay` recomputes each item's
`ItemState` from the log in time order, so the reviews of two PCs (the log merges by union) give
the same schedule on both.

`schedule` is an SM-2 variant with four ratings (`again`, `hard`, `good`, `easy`): the ease
starts at `INITIAL_EASE` and never goes under `MIN_EASE`; `again` resets the repetitions and brings
the item back after `RELEARN_DELAY`; the first `good` is due in 1 day, the second in 6, then the
interval grows by the ease; `hard` multiplies it by 1.2 and `easy` by ease × 1.3; no interval is
longer than `MAX_INTERVAL_DAYS`.

`practice_queue` tells what to practise now (due items, oldest first, then up to `new_limit` items
never seen, minus the new ones already started that day); `record_practice_review` grades a quiz
answer with the quiz's own `grade` (a wrong answer is `again`, a right one `good` unless the
student picks `hard` or `easy`), takes the student's rating for a flashcard, stores the review and
answers the item's new schedule.

The student can set an item aside ("esta tarjeta no me sirve", #281): `suspend_practice_item`
appends a `PracticeSuspension` (`action: suspend`) to the same log and `restore_practice_item` one
with `action: restore`. The latest of them per item, in time order (file order on a tie), says
whether it is suspended, so two PCs' interleaved lines agree. A suspended item is never queued
(neither due nor new) and is counted apart; its reviews stay in the log, so a restored item comes
back with its previous history and schedule. Setting aside an item already set aside (or restoring
one that is not) writes nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from studentassistant.generators import flashcards as flashcards_module
from studentassistant.generators.quiz import (
    GradedAnswer,
    QuestionType,
    QuizAnswer,
    QuizQuestion,
    grade,
    normalize_answer,
    read_quiz,
)
from studentassistant.generators.registry import default_registry
from studentassistant.generators.run import GenerationError, artifact_status
from studentassistant.vault import GitSync, Vault, read_generated
from studentassistant.vault.study import append_study_record, read_study_records

PRACTICE_LOG = "practice"
FLASHCARDS_SOURCE = "flashcards"
QUIZ_SOURCE = "quiz"

INITIAL_EASE = 2.5
MIN_EASE = 1.3
MAX_EASE = 3.5
RELEARN_DELAY = timedelta(minutes=10)
FIRST_INTERVAL_DAYS = 1.0
SECOND_INTERVAL_DAYS = 6.0
EASY_FIRST_INTERVAL_DAYS = 4.0
HARD_FACTOR = 1.2
EASY_BONUS = 1.3
MAX_INTERVAL_DAYS = 365.0
DEFAULT_NEW_LIMIT = 10
MAX_NEW_LIMIT = 100

Rating = Literal["again", "hard", "good", "easy"]
Source = Literal["flashcards", "quiz"]

Clock = Callable[[], datetime]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- the scheduler ---------------------------------------------------------------------------------


class ItemState(_Strict):
    """Where an item stands after its reviews (recomputed, never stored)."""

    reviews: int = Field(default=0, description="Every review, `again` included.")
    repetitions: int = Field(default=0, description="Successful reviews in a row.")
    lapses: int = Field(default=0, description="Times it was forgotten after being learned.")
    ease: float = INITIAL_EASE
    interval_days: float = Field(default=0.0, description="0 while (re)learning.")
    first_review: datetime | None = None
    last_review: datetime | None = None
    last_rating: Rating | None = None
    due: datetime | None = Field(default=None, description="None for an item never reviewed.")


def schedule(state: ItemState, rating: Rating, now: datetime) -> ItemState:
    """`state` after a review rated `rating` at `now` (pure)."""
    ease = state.ease
    repetitions = state.repetitions
    lapses = state.lapses
    if rating == "again":
        if repetitions > 0:
            lapses += 1
        ease = max(MIN_EASE, ease - 0.2)
        return state.model_copy(
            update={
                "reviews": state.reviews + 1,
                "repetitions": 0,
                "lapses": lapses,
                "ease": ease,
                "interval_days": 0.0,
                "first_review": state.first_review or now,
                "last_review": now,
                "last_rating": rating,
                "due": now + RELEARN_DELAY,
            }
        )
    if rating == "hard":
        ease = max(MIN_EASE, ease - 0.15)
        interval = (
            FIRST_INTERVAL_DAYS if repetitions == 0 else max(1.0, state.interval_days * HARD_FACTOR)
        )
    elif rating == "good":
        if repetitions == 0:
            interval = FIRST_INTERVAL_DAYS
        elif repetitions == 1:
            interval = SECOND_INTERVAL_DAYS
        else:
            interval = state.interval_days * ease
    else:  # easy
        interval = (
            EASY_FIRST_INTERVAL_DAYS
            if repetitions == 0
            else max(SECOND_INTERVAL_DAYS, state.interval_days * ease * EASY_BONUS)
        )
        ease = min(MAX_EASE, ease + 0.15)
    interval = round(min(MAX_INTERVAL_DAYS, interval), 4)
    return state.model_copy(
        update={
            "reviews": state.reviews + 1,
            "repetitions": repetitions + 1,
            "lapses": lapses,
            "ease": round(ease, 4),
            "interval_days": interval,
            "first_review": state.first_review or now,
            "last_review": now,
            "last_rating": rating,
            "due": now + timedelta(days=interval),
        }
    )


# -- the history -----------------------------------------------------------------------------------


class PracticeReview(_Strict):
    """One line of `study/practice.jsonl`."""

    time: datetime
    item: str = Field(description="`flashcards:<card id>` or `quiz:<question hash>`.")
    source: Source
    rating: Rating
    given: str | None = Field(default=None, description="Quiz questions: the answer given.")
    correct: bool | None = Field(default=None, description="Quiz questions: graded right.")
    graded_by: Literal["auto", "student"] | None = None
    anchors: list[str] = Field(default_factory=list)


class PracticeSuspension(_Strict):
    """A line of `study/practice.jsonl` that sets an item aside or brings it back (#281)."""

    time: datetime
    item: str = Field(description="`flashcards:<card id>` or `quiz:<question hash>`.")
    source: Source
    action: Literal["suspend", "restore"]


class _PracticeLine(RootModel[PracticeReview | PracticeSuspension]):
    """Any line of the practice log: `extra="forbid"` tells the two kinds apart."""


def replay(reviews: Iterable[PracticeReview]) -> dict[str, ItemState]:
    """Every reviewed item's state, from its reviews in time order (file order on a tie)."""
    states: dict[str, ItemState] = {}
    ordered = sorted(enumerate(reviews), key=lambda pair: (pair[1].time, pair[0]))
    for _, review in ordered:
        states[review.item] = schedule(
            states.get(review.item, ItemState()), review.rating, review.time
        )
    return states


def practice_history(vault: Vault, subject: str, topic: str) -> list[PracticeReview]:
    """Every review of the topic's practice, in file order."""
    lines = practice_log(vault, subject, topic)
    return [line for line in lines if isinstance(line, PracticeReview)]


def practice_log(
    vault: Vault, subject: str, topic: str
) -> list[PracticeReview | PracticeSuspension]:
    """Every line of the topic's practice log (reviews and suspensions), in file order."""
    lines = read_study_records(vault, subject, topic, PRACTICE_LOG, _PracticeLine)
    return [line.root for line in lines]


def suspensions(lines: Iterable[PracticeReview | PracticeSuspension]) -> dict[str, datetime]:
    """The suspended items and when each was set aside: the latest record per item wins."""
    records = [line for line in lines if isinstance(line, PracticeSuspension)]
    latest: dict[str, PracticeSuspension] = {}
    for _, record in sorted(enumerate(records), key=lambda pair: (pair[1].time, pair[0])):
        latest[record.item] = record
    return {key: record.time for key, record in latest.items() if record.action == "suspend"}


# -- the items -------------------------------------------------------------------------------------


class PracticeItem(_Strict):
    """A flashcard or a quiz question as the practice shows it."""

    key: str
    source: Source
    prompt: str = Field(description="The card's front or the question.")
    answer: str = Field(description="The card's back or the expected answer.")
    question_type: QuestionType | None = Field(default=None, description="Quiz questions only.")
    options: list[str] = Field(default_factory=list)
    explanation: str = ""
    anchors: list[str] = Field(default_factory=list)


def quiz_item_key(question: str) -> str:
    """A quiz question's key: the same question (normalized) in any regeneration keeps it."""
    digest = hashlib.sha256(normalize_answer(question).encode("utf-8")).hexdigest()
    return f"{QUIZ_SOURCE}:{digest[:12]}"


def flashcard_item_key(card_id: str) -> str:
    return f"{FLASHCARDS_SOURCE}:{card_id}"


class _Material(_Strict):
    items: list[PracticeItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    kinds: list[Source] = Field(default_factory=list)


def _flashcards(vault: Vault, subject: str, topic: str) -> _Material:
    data = read_generated(vault, subject, topic, flashcards_module.YAML_NAME)
    if data is None:
        return _Material()
    try:
        deck = flashcards_module.parse_flashcards(data)
    except ValueError as error:
        raise GenerationError(f"No se pueden leer las flashcards: {error}") from error
    warnings = []
    status = artifact_status(
        vault, subject, topic, flashcards_module.KIND, registry=default_registry
    )
    if status.stale:
        warnings.append(
            "Las flashcards son de una versión anterior de los apuntes"
            + (f" ({status.stale_reason})" if status.stale_reason else "")
            + ": puedes generarlas de nuevo."
        )
    items = [
        PracticeItem(
            key=flashcard_item_key(card.id),
            source=FLASHCARDS_SOURCE,
            prompt=card.front,
            answer=card.back,
            anchors=card.anchors,
        )
        for card in deck.cards
    ]
    return _Material(items=items, warnings=warnings, kinds=[FLASHCARDS_SOURCE])


def _quiz(vault: Vault, subject: str, topic: str) -> _Material:
    stored = read_quiz(vault, subject, topic)
    if stored is None:
        return _Material()
    warnings = []
    if stored.stale:
        warnings.append(
            "El quiz es de una versión anterior de los apuntes"
            + (f" ({stored.stale_reason})" if stored.stale_reason else "")
            + ": puedes generarlo de nuevo."
        )
    items: list[PracticeItem] = []
    seen: set[str] = set()
    for question in stored.quiz.questions:
        key = quiz_item_key(question.question)
        if key in seen:  # the same question twice in one quiz: practised once
            continue
        seen.add(key)
        items.append(
            PracticeItem(
                key=key,
                source=QUIZ_SOURCE,
                prompt=question.question,
                answer=question.answer,
                question_type=question.type,
                options=question.options,
                explanation=question.explanation,
                anchors=question.anchors,
            )
        )
    return _Material(items=items, warnings=warnings, kinds=[QUIZ_SOURCE])


def practice_items(vault: Vault, subject: str, topic: str) -> tuple[list[PracticeItem], list[str]]:
    """The topic's current practice items (flashcards, then quiz questions) and Spanish warnings.

    Raises:
        GenerationError: a material cannot be read.
        VaultError: the topic cannot be read.
    """
    cards = _flashcards(vault, subject, topic)
    quiz = _quiz(vault, subject, topic)
    warnings = cards.warnings + quiz.warnings
    if not cards.kinds and not quiz.kinds:
        warnings.append("Todavía no hay flashcards ni quiz de este tema: genéralos para practicar.")
    return cards.items + quiz.items, warnings


# -- the queue -------------------------------------------------------------------------------------


class QueuedItem(_Strict):
    item: PracticeItem
    state: ItemState | None = Field(default=None, description="None for an item never reviewed.")


class PracticeCounts(_Strict):
    total: int = Field(description="Items in the current material, suspended ones included.")
    due: int = Field(description="Reviewed items due now.")
    new: int = Field(description="Never-reviewed items offered now (within the daily limit).")
    unseen: int = Field(description="Every never-reviewed item not suspended.")
    learned: int = Field(description="Items reviewed at least once, not suspended.")
    new_today: int = Field(description="Items first reviewed today.")
    suspended: int = Field(default=0, description="Items set aside: never queued.")


class SuspendedItem(_Strict):
    """A current item the student set aside, as the practice page lists it."""

    key: str
    source: Source
    prompt: str
    suspended_at: datetime


class PracticeQueue(_Strict):
    """What to practise now: due items (oldest due first), then new items."""

    now: datetime
    queue: list[QueuedItem]
    counts: PracticeCounts
    next_due: datetime | None = Field(
        default=None, description="The earliest due time after `now` of a reviewed item."
    )
    suspended: list[SuspendedItem] = Field(
        default_factory=list, description="The items set aside, the latest first."
    )
    warnings: list[str] = Field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _local_day(moment: datetime, tz: tzinfo | None) -> date:
    return moment.astimezone(tz).date()


def practice_queue(
    vault: Vault,
    subject: str,
    topic: str,
    *,
    now: datetime | None = None,
    new_limit: int = DEFAULT_NEW_LIMIT,
    tz: tzinfo | None = None,
) -> PracticeQueue:
    """The topic's practice queue at `now`.

    `new_limit` is how many never-seen items a day may start; the ones already started on the
    day of `now` (in `tz`, the machine's local time zone by default) count against it.

    Raises:
        GenerationError: a material cannot be read.
        VaultError, JsonlError: the topic or its practice log cannot be read.
    """
    moment = now or _utc_now()
    items, warnings = practice_items(vault, subject, topic)
    lines = practice_log(vault, subject, topic)
    states = replay(line for line in lines if isinstance(line, PracticeReview))
    aside = suspensions(lines)
    today = _local_day(moment, tz)
    current = {item.key for item in items}
    new_today = sum(
        1
        for key, state in states.items()
        if key in current
        and state.first_review is not None
        and _local_day(state.first_review, tz) == today
    )
    due: list[QueuedItem] = []
    unseen: list[QueuedItem] = []
    later: list[datetime] = []
    suspended = _suspended(items, aside)
    for item in items:
        state = states.get(item.key)
        if item.key in aside:
            continue
        if state is None:
            unseen.append(QueuedItem(item=item))
        elif state.due is not None and state.due <= moment:
            due.append(QueuedItem(item=item, state=state))
        elif state.due is not None:
            later.append(state.due)
    due.sort(key=lambda queued: queued.state.due if queued.state and queued.state.due else moment)
    fresh = unseen[: max(0, new_limit - new_today)]
    return PracticeQueue(
        now=moment,
        queue=due + fresh,
        counts=PracticeCounts(
            total=len(items),
            due=len(due),
            new=len(fresh),
            unseen=len(unseen),
            learned=len(items) - len(unseen) - len(suspended),
            new_today=new_today,
            suspended=len(suspended),
        ),
        next_due=min(later) if later else None,
        suspended=suspended,
        warnings=warnings,
    )


def _suspended(items: list[PracticeItem], aside: dict[str, datetime]) -> list[SuspendedItem]:
    listed = [
        SuspendedItem(
            key=item.key, source=item.source, prompt=item.prompt, suspended_at=aside[item.key]
        )
        for item in items
        if item.key in aside
    ]
    return sorted(listed, key=lambda entry: entry.suspended_at, reverse=True)


def suspended_items(vault: Vault, subject: str, topic: str) -> list[SuspendedItem]:
    """The current items the student set aside, the latest first.

    An item set aside that left the material is not listed (it is not offered anyway); it stays
    set aside if it comes back.

    Raises:
        GenerationError: a material cannot be read.
        VaultError, JsonlError: the topic or its practice log cannot be read.
    """
    items, _warnings = practice_items(vault, subject, topic)
    return _suspended(items, suspensions(practice_log(vault, subject, topic)))


# -- reviewing -------------------------------------------------------------------------------------


class PracticeItemNotFoundError(GenerationError):
    def __init__(self, key: str) -> None:
        super().__init__(
            f"El elemento «{key}» ya no está en las flashcards ni en el quiz de este tema."
        )


class InvalidReviewError(GenerationError):
    pass


class PracticeAnswer(_Strict):
    """One review, as the web sends it."""

    item: str = Field(description="The item's key.")
    rating: Rating | None = Field(
        default=None,
        description="Flashcards: required. Quiz: `hard`/`easy` for a right answer, else ignored.",
    )
    given: str | None = Field(default=None, description="Quiz: the option chosen or text written.")
    self_assessed: bool | None = Field(
        default=None, description="Quiz short answer: the student's verdict after seeing it."
    )


class ReviewOutcome(_Strict):
    review: PracticeReview
    state: ItemState


def record_practice_review(
    vault: Vault,
    subject: str,
    topic: str,
    answer: PracticeAnswer,
    *,
    sync: GitSync,
    clock: Clock = _utc_now,
) -> ReviewOutcome:
    """Grade and store one review of a current practice item, answer its new state.

    The record is on disk when this returns; the commit is left to `sync` (`note_change()`), so
    the reviews of one sitting land in one commit (`run_due()`, `flush()`).

    Raises:
        PracticeItemNotFoundError: no current flashcard or quiz question has that key.
        InvalidReviewError: a flashcard review without a rating.
        GenerationError, VaultError: the material or the topic cannot be read or written.
    """
    items, _warnings = practice_items(vault, subject, topic)
    item = next((candidate for candidate in items if candidate.key == answer.item), None)
    if item is None:
        raise PracticeItemNotFoundError(answer.item)
    now = clock()
    if item.source == FLASHCARDS_SOURCE:
        if answer.rating is None:
            raise InvalidReviewError(
                "Di cómo te ha ido con la tarjeta (otra vez, difícil, bien o fácil)."
            )
        review = PracticeReview(
            time=now, item=item.key, source=item.source, rating=answer.rating, anchors=item.anchors
        )
    else:
        graded = _grade_quiz_item(item, answer)
        rating: Rating = "again"
        if graded.correct:
            rating = answer.rating if answer.rating in ("hard", "easy") else "good"
        review = PracticeReview(
            time=now,
            item=item.key,
            source=item.source,
            rating=rating,
            given=graded.given,
            correct=graded.correct,
            graded_by=graded.graded_by,
            anchors=item.anchors,
        )
    append_study_record(vault, subject, topic, PRACTICE_LOG, review)
    # The reviews of one sitting are committed together by the sync's quiet-period batch.
    sync.note_change()
    state = replay(practice_history(vault, subject, topic)).get(item.key)
    assert state is not None  # just appended
    return ReviewOutcome(review=review, state=state)


def _grade_quiz_item(item: PracticeItem, answer: PracticeAnswer) -> GradedAnswer:
    """The answer graded as the quiz page grades it (`quiz.grade`)."""
    question = QuizQuestion(
        id=item.key,
        type=item.question_type or "short_answer",
        difficulty="medium",
        question=item.prompt,
        options=item.options,
        answer=item.answer,
        explanation=item.explanation,
        anchors=item.anchors,
    )
    return grade(
        question,
        QuizAnswer(question=item.key, given=answer.given, self_assessed=answer.self_assessed),
    )


# -- setting items aside ---------------------------------------------------------------------------


class SuspensionOutcome(_Strict):
    """Where an item stands after a suspend or restore request."""

    item: str
    suspended: bool
    suspended_at: datetime | None = Field(default=None, description="When, while set aside.")
    changed: bool = Field(description="False when it already was so: nothing was written.")


def suspend_practice_item(
    vault: Vault, subject: str, topic: str, key: str, *, sync: GitSync, clock: Clock = _utc_now
) -> SuspensionOutcome:
    """Set a current item aside so it is no longer queued (idempotent).

    Raises:
        PracticeItemNotFoundError: no current flashcard or quiz question has that key.
        GenerationError, VaultError: the material or the topic cannot be read or written.
    """
    return _set_suspended(vault, subject, topic, key, True, sync=sync, clock=clock)


def restore_practice_item(
    vault: Vault, subject: str, topic: str, key: str, *, sync: GitSync, clock: Clock = _utc_now
) -> SuspensionOutcome:
    """Bring a set-aside item back, with its previous history and schedule (idempotent).

    Raises:
        PracticeItemNotFoundError: no current flashcard or quiz question has that key.
        GenerationError, VaultError: the material or the topic cannot be read or written.
    """
    return _set_suspended(vault, subject, topic, key, False, sync=sync, clock=clock)


def _set_suspended(
    vault: Vault,
    subject: str,
    topic: str,
    key: str,
    suspend: bool,
    *,
    sync: GitSync,
    clock: Clock,
) -> SuspensionOutcome:
    items, _warnings = practice_items(vault, subject, topic)
    item = next((candidate for candidate in items if candidate.key == key), None)
    if item is None:
        raise PracticeItemNotFoundError(key)
    since = suspensions(practice_log(vault, subject, topic)).get(key)
    if (since is not None) == suspend:
        return SuspensionOutcome(item=key, suspended=suspend, suspended_at=since, changed=False)
    now = clock()
    record = PracticeSuspension(
        time=now, item=key, source=item.source, action="suspend" if suspend else "restore"
    )
    append_study_record(vault, subject, topic, PRACTICE_LOG, record)
    sync.note_change()
    return SuspensionOutcome(
        item=key, suspended=suspend, suspended_at=now if suspend else None, changed=True
    )
