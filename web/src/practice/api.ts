/**
 * Client of the practice API (#81, `docs/modules/generators.md`):
 * - `GET /api/subjects/{s}/topics/{t}/practice` -> `PracticeQueue`: the items due now, then the
 *   new ones of the day, with counts, the next due time and Spanish warnings.
 * - `POST .../practice/reviews`, body `PracticeAnswer` -> `ReviewOutcome`: the review (graded by
 *   the backend for a quiz question) and the item's new schedule, kept in `study/practice.jsonl`.
 * - `POST .../practice/items/{key}/suspend` and `.../restore` -> `SuspensionOutcome` (#281): set an
 *   item aside (no longer queued) or bring it back with its history; idempotent. The items set
 *   aside come in the queue response (`suspended`).
 *
 * Every call answers an `ActionResult` (`pending/doubts.ts`). Bodies are read leniently: an item
 * the page cannot show is skipped, a body without the fields the page needs is an error.
 */

import { topicPath } from "../desk/api";
import { type ActionResult, postAction } from "../pending/doubts";
import { getAction, type QuestionType } from "../quiz/api";

export type Rating = "again" | "hard" | "good" | "easy";
export type Source = "flashcards" | "quiz";

export const RATINGS: readonly Rating[] = ["again", "hard", "good", "easy"];
export const RATING_LABELS: Record<Rating, string> = {
  again: "Otra vez",
  hard: "Difícil",
  good: "Bien",
  easy: "Fácil",
};

export interface PracticeItem {
  key: string;
  source: Source;
  prompt: string;
  answer: string;
  question_type: QuestionType | null;
  options: string[];
  explanation: string;
  anchors: string[];
}

export interface ItemSchedule {
  reviews: number;
  interval_days: number;
  due: string | null;
}

export interface QueuedItem {
  item: PracticeItem;
  /** `null` for an item never reviewed. */
  state: ItemSchedule | null;
}

export interface PracticeCounts {
  total: number;
  due: number;
  new: number;
  unseen: number;
  learned: number;
  suspended: number;
}

export interface SuspendedItem {
  key: string;
  source: Source;
  prompt: string;
  suspended_at: string;
}

export interface SuspensionOutcome {
  item: string;
  suspended: boolean;
}

export interface PracticeQueue {
  queue: QueuedItem[];
  counts: PracticeCounts;
  suspended: SuspendedItem[];
  next_due: string | null;
  warnings: string[];
}

export interface PracticeAnswer {
  item: string;
  rating?: Rating;
  given?: string | null;
  self_assessed?: boolean;
}

export interface ReviewOutcome {
  rating: Rating;
  correct: boolean | null;
  state: ItemSchedule;
}

const QUESTION_TYPES: readonly QuestionType[] = ["multiple_choice", "true_false", "short_answer"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

function readItem(value: unknown): PracticeItem | null {
  if (!isRecord(value) || typeof value.key !== "string") return null;
  if (typeof value.prompt !== "string" || typeof value.answer !== "string") return null;
  const source = (["flashcards", "quiz"] as const).find((s) => s === value.source);
  if (source === undefined) return null;
  const questionType = QUESTION_TYPES.find((t) => t === value.question_type) ?? null;
  if (source === "quiz" && questionType === null) return null;
  const options = texts(value.options);
  if (questionType !== null && questionType !== "short_answer" && options.length < 2) return null;
  return {
    key: value.key,
    source,
    prompt: value.prompt,
    answer: value.answer,
    question_type: questionType,
    options,
    explanation: typeof value.explanation === "string" ? value.explanation : "",
    anchors: texts(value.anchors),
  };
}

function readSchedule(value: unknown): ItemSchedule | null {
  if (!isRecord(value)) return null;
  return {
    reviews: count(value.reviews),
    interval_days: count(value.interval_days),
    due: typeof value.due === "string" ? value.due : null,
  };
}

function readSuspended(value: unknown): SuspendedItem | null {
  if (!isRecord(value) || typeof value.key !== "string" || typeof value.prompt !== "string") return null;
  const source = (["flashcards", "quiz"] as const).find((s) => s === value.source);
  if (source === undefined) return null;
  const at = typeof value.suspended_at === "string" ? value.suspended_at : "";
  return { key: value.key, source, prompt: value.prompt, suspended_at: at };
}

export function readSuspension(body: unknown): SuspensionOutcome | null {
  if (!isRecord(body) || typeof body.item !== "string" || typeof body.suspended !== "boolean") return null;
  return { item: body.item, suspended: body.suspended };
}

export function readQueue(body: unknown): PracticeQueue | null {
  if (!isRecord(body) || !Array.isArray(body.queue) || !isRecord(body.counts)) return null;
  const queue: QueuedItem[] = [];
  for (const entry of body.queue) {
    if (!isRecord(entry)) continue;
    const item = readItem(entry.item);
    if (item !== null) queue.push({ item, state: readSchedule(entry.state) });
  }
  const counts = body.counts;
  const suspended = Array.isArray(body.suspended)
    ? body.suspended.map(readSuspended).filter((item): item is SuspendedItem => item !== null)
    : [];
  return {
    queue,
    suspended,
    counts: {
      total: count(counts.total),
      due: count(counts.due),
      new: count(counts.new),
      unseen: count(counts.unseen),
      learned: count(counts.learned),
      suspended: count(counts.suspended),
    },
    next_due: typeof body.next_due === "string" ? body.next_due : null,
    warnings: texts(body.warnings),
  };
}

export function readOutcome(body: unknown): ReviewOutcome | null {
  if (!isRecord(body) || !isRecord(body.review)) return null;
  const rating = RATINGS.find((r) => r === (body.review as Record<string, unknown>).rating);
  const state = readSchedule(body.state);
  if (rating === undefined || state === null) return null;
  const correct = body.review.correct;
  return { rating, correct: typeof correct === "boolean" ? correct : null, state };
}

export function practicePath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/practice`;
}

export function fetchPractice(subjectId: string, topicId: string): Promise<ActionResult<PracticeQueue>> {
  return getAction(practicePath(subjectId, topicId), readQueue);
}

export function sendReview(
  subjectId: string,
  topicId: string,
  answer: PracticeAnswer,
): Promise<ActionResult<ReviewOutcome>> {
  return postAction(`${practicePath(subjectId, topicId)}/reviews`, answer, readOutcome);
}

/** Set an item aside (`suspend: true`) or bring it back. */
export function sendSuspension(
  subjectId: string,
  topicId: string,
  key: string,
  suspend: boolean,
): Promise<ActionResult<SuspensionOutcome>> {
  const path = `${practicePath(subjectId, topicId)}/items/${encodeURIComponent(key)}/${suspend ? "suspend" : "restore"}`;
  return postAction(path, undefined, readSuspension);
}

/** "en 10 minutos", "mañana", "en 6 días", "en 2 meses": when an item comes back. */
export function describeInterval(days: number): string {
  if (days < 1 / 24) {
    const minutes = Math.max(1, Math.round(days * 24 * 60));
    return minutes === 1 ? "en 1 minuto" : `en ${minutes} minutos`;
  }
  if (days < 1) {
    const hours = Math.round(days * 24);
    return hours === 1 ? "en 1 hora" : `en ${hours} horas`;
  }
  const whole = Math.round(days);
  if (whole === 1) return "mañana";
  if (whole < 45) return `en ${whole} días`;
  const months = Math.round(days / 30);
  return months === 1 ? "en 1 mes" : `en ${months} meses`;
}
