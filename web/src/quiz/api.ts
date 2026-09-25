/**
 * Client of the quiz API (#75, `docs/modules/generators.md`):
 * - `GET /api/subjects/{s}/topics/{t}/quiz` -> `StoredQuiz` (404 when there is none yet).
 * - `POST .../generated/quiz` (the generators API, #67), body `{options, confirm_over_cap}`.
 * - `POST .../quiz/results`, body `QuizAttempt` -> `QuizResult` (graded by the backend, kept in
 *   `study/quiz-results.jsonl`); 409 when the quiz was generated again meanwhile. A partial attempt
 *   (`questions`: the ids asked, #282) is graded on those only and recorded as such.
 * - `GET .../quiz/results` -> `QuizResult[]`, oldest first.
 *
 * Every call answers an `ActionResult` (`pending/doubts.ts`); a refusal keeps the backend's Spanish
 * `detail`. Bodies are read leniently: a question the page cannot show is skipped, a body without
 * the fields the page needs is an error.
 */

import { topicPath } from "../desk/api";
import { errorCode } from "../protocol";
import { type ActionResult, isOverCap, postAction } from "../pending/doubts";

export type QuestionType = "multiple_choice" | "true_false" | "short_answer";
export type Difficulty = "easy" | "medium" | "hard";
export type QuizDifficulty = Difficulty | "mixed";

export interface QuizQuestion {
  id: string;
  type: QuestionType;
  difficulty: Difficulty;
  question: string;
  options: string[];
  answer: string;
  explanation: string;
  anchors: string[];
}

export interface StoredQuiz {
  title: string;
  difficulty: QuizDifficulty;
  questions: QuizQuestion[];
  /** ISO 8601: identifies the quiz an attempt answers. */
  built_at: string;
  notes_version: number | null;
  stale: boolean;
  stale_reason: string | null;
}

export interface QuizAnswer {
  question: string;
  given: string | null;
  /** Short answers only: the student's verdict after seeing the answer. */
  self_assessed?: boolean;
}

export interface QuizAttempt {
  built_at: string;
  answers: QuizAnswer[];
  duration_seconds?: number;
  /** A partial attempt (retaking the questions answered wrong): the ids asked. */
  questions?: string[];
}

export interface QuizResult {
  time: string;
  total: number;
  correct: number;
  /** Whether only some questions were asked (the backend's `questions` is a list). */
  partial: boolean;
}

export interface QuizOptions {
  size: number;
  difficulty: QuizDifficulty;
}

export const DIFFICULTY_LABELS: Record<QuizDifficulty, string> = {
  mixed: "Variada",
  easy: "Fácil",
  medium: "Media",
  hard: "Difícil",
};

const TYPES: readonly QuestionType[] = ["multiple_choice", "true_false", "short_answer"];
const DIFFICULTIES: readonly Difficulty[] = ["easy", "medium", "hard"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function readQuestion(entry: unknown): QuizQuestion | null {
  if (!isRecord(entry) || typeof entry.id !== "string" || typeof entry.question !== "string") return null;
  const type = TYPES.find((t) => t === entry.type);
  if (type === undefined || typeof entry.answer !== "string") return null;
  const options = texts(entry.options);
  if (type !== "short_answer" && options.length < 2) return null;
  return {
    id: entry.id,
    type,
    difficulty: DIFFICULTIES.find((d) => d === entry.difficulty) ?? "medium",
    question: entry.question,
    options,
    answer: entry.answer,
    explanation: text(entry.explanation),
    anchors: texts(entry.anchors),
  };
}

export function readStoredQuiz(body: unknown): StoredQuiz | null {
  if (!isRecord(body) || !isRecord(body.quiz) || typeof body.built_at !== "string") return null;
  const quiz = body.quiz;
  if (!Array.isArray(quiz.questions)) return null;
  const questions = quiz.questions.map(readQuestion).filter((q): q is QuizQuestion => q !== null);
  const difficulty = (["mixed", ...DIFFICULTIES] as const).find((d) => d === quiz.difficulty) ?? "mixed";
  return {
    title: text(quiz.title, "Quiz"),
    difficulty,
    questions,
    built_at: body.built_at,
    notes_version: count(body.notes_version),
    stale: body.stale === true,
    stale_reason: typeof body.stale_reason === "string" ? body.stale_reason : null,
  };
}

export function readResult(body: unknown): QuizResult | null {
  if (!isRecord(body) || typeof body.time !== "string") return null;
  const total = count(body.total);
  const correct = count(body.correct);
  if (total === null || correct === null) return null;
  return { time: body.time, total, correct, partial: Array.isArray(body.questions) };
}

export function readResults(body: unknown): QuizResult[] | null {
  if (!Array.isArray(body)) return null;
  return body.map(readResult).filter((r): r is QuizResult => r !== null);
}

/** Lowercase, no accents, single spaces, no surrounding punctuation (as the backend grades). */
export function normalizeAnswer(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[\s.,;:¡!¿?"'«»()]+|[\s.,;:¡!¿?"'«»()]+$/g, "");
}

export function quizPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/quiz`;
}

export async function getAction<T>(path: string, read: (body: unknown) => T | null): Promise<ActionResult<T>> {
  let response: Response;
  try {
    response = await fetch(path);
  } catch {
    return { kind: "unreachable" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    const detail = isRecord(body) ? body.detail : undefined;
    if (typeof detail === "string" && detail !== "") {
      const code = errorCode(body);
      return { kind: "refused", status: response.status, detail, code, overCap: isOverCap(code) };
    }
    return { kind: "error", status: response.status };
  }
  const value = read(body);
  return value === null ? { kind: "error", status: response.status } : { kind: "ok", value };
}

export function fetchQuiz(subjectId: string, topicId: string): Promise<ActionResult<StoredQuiz>> {
  return getAction(quizPath(subjectId, topicId), readStoredQuiz);
}

export function fetchQuizResults(subjectId: string, topicId: string): Promise<ActionResult<QuizResult[]>> {
  return getAction(`${quizPath(subjectId, topicId)}/results`, readResults);
}

export function generateQuiz(
  subjectId: string,
  topicId: string,
  options: QuizOptions,
  confirmOverCap = false,
): Promise<ActionResult<true>> {
  return postAction(
    `/api${topicPath(subjectId, topicId)}/generated/quiz`,
    { options, confirm_over_cap: confirmOverCap },
    (body) => (isRecord(body) && body.kind === "quiz" ? true : null),
  );
}

export function saveQuizResult(
  subjectId: string,
  topicId: string,
  attempt: QuizAttempt,
): Promise<ActionResult<QuizResult>> {
  return postAction(`${quizPath(subjectId, topicId)}/results`, attempt, readResult);
}
