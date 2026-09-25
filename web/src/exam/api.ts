/**
 * Client of the exam correction API (#283, `docs/modules/generators.md`):
 * - `GET /api/subjects/{s}/topics/{t}/exam` -> `StoredExam` (`examen.yaml` with its manifest's
 *   `built_at` and staleness); 404 when there is no exam to correct yet.
 * - `POST .../exam/results`, body `ExamAttempt` (points per rubric criterion) -> `ExamResult`
 *   (score, total and percentage computed by the backend, kept in `study/exam-results.jsonl`);
 *   409 when the exam was generated again meanwhile, 422 points out of range.
 * - `GET .../exam/results` -> `ExamResult[]`, oldest first.
 *
 * Every call answers an `ActionResult` (`pending/doubts.ts`); a refusal keeps the backend's Spanish
 * `detail`. Bodies are read leniently: a question the page cannot show is skipped, a body without
 * the fields the page needs is an error.
 */

import { topicPath } from "../desk/api";
import { type ActionResult, postAction } from "../pending/doubts";
import { getAction } from "../quiz/api";

export interface Criterion {
  criterion: string;
  points: number;
}

export interface ExamQuestion {
  id: string;
  number: number;
  statement: string;
  /** What the question is worth: its points, else its rubric added up. */
  points: number;
  solution: string;
  /** The criteria it is graded by; a question without rubric is one whole criterion. */
  criteria: Criterion[];
  anchors: string[];
}

export interface StoredExam {
  title: string;
  instructions: string;
  durationMinutes: number | null;
  questions: ExamQuestion[];
  /** ISO 8601: identifies the exam a correction answers. */
  built_at: string;
  notes_version: number | null;
  stale: boolean;
  stale_reason: string | null;
}

export interface ExamAttempt {
  built_at: string;
  questions: { question: string; awarded: number[] }[];
}

export interface ExamResult {
  time: string;
  score: number;
  total: number;
  percentage: number;
}

/** The label of the single criterion of a question without rubric (as the backend names it). */
export const WHOLE_QUESTION = "Pregunta completa";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function points(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function readCriterion(entry: unknown): Criterion | null {
  if (!isRecord(entry) || typeof entry.criterion !== "string") return null;
  const value = points(entry.points);
  return value === null ? null : { criterion: entry.criterion, points: value };
}

function readQuestion(entry: unknown, index: number): ExamQuestion | null {
  if (!isRecord(entry) || typeof entry.id !== "string" || typeof entry.statement !== "string") return null;
  const rubric = Array.isArray(entry.rubric)
    ? entry.rubric.map(readCriterion).filter((c): c is Criterion => c !== null)
    : [];
  const worth = points(entry.points) ?? rubric.reduce((sum, c) => sum + c.points, 0);
  return {
    id: entry.id,
    number: typeof entry.number === "number" ? entry.number : index + 1,
    statement: entry.statement,
    points: worth,
    solution: typeof entry.solution === "string" ? entry.solution : "",
    criteria: rubric.length > 0 ? rubric : [{ criterion: WHOLE_QUESTION, points: worth }],
    anchors: texts(entry.anchors),
  };
}

export function readStoredExam(body: unknown): StoredExam | null {
  if (!isRecord(body) || !isRecord(body.exam) || typeof body.built_at !== "string") return null;
  const exam = body.exam;
  if (!Array.isArray(exam.questions)) return null;
  const questions = exam.questions
    .map((entry, index) => readQuestion(entry, index))
    .filter((q): q is ExamQuestion => q !== null);
  const notesVersion = body.notes_version;
  return {
    title: typeof exam.title === "string" ? exam.title : "Examen",
    instructions: typeof exam.instructions === "string" ? exam.instructions : "",
    durationMinutes: typeof exam.duration_minutes === "number" ? exam.duration_minutes : null,
    questions,
    built_at: body.built_at,
    notes_version: typeof notesVersion === "number" && Number.isInteger(notesVersion) ? notesVersion : null,
    stale: body.stale === true,
    stale_reason: typeof body.stale_reason === "string" ? body.stale_reason : null,
  };
}

export function readResult(body: unknown): ExamResult | null {
  if (!isRecord(body) || typeof body.time !== "string") return null;
  const score = points(body.score);
  const total = points(body.total);
  const percentage = points(body.percentage);
  if (score === null || total === null || percentage === null) return null;
  return { time: body.time, score, total, percentage };
}

export function readResults(body: unknown): ExamResult[] | null {
  if (!Array.isArray(body)) return null;
  return body.map(readResult).filter((r): r is ExamResult => r !== null);
}

/** `2.5` -> `2,5`, `10` -> `10` (at most two decimals, Spanish decimal comma). */
export function formatNumber(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return String(rounded).replace(".", ",");
}

export function examPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/exam`;
}

export function fetchExam(subjectId: string, topicId: string): Promise<ActionResult<StoredExam>> {
  return getAction(examPath(subjectId, topicId), readStoredExam);
}

export function fetchExamResults(subjectId: string, topicId: string): Promise<ActionResult<ExamResult[]>> {
  return getAction(`${examPath(subjectId, topicId)}/results`, readResults);
}

export function saveExamResult(
  subjectId: string,
  topicId: string,
  attempt: ExamAttempt,
): Promise<ActionResult<ExamResult>> {
  return postAction(`${examPath(subjectId, topicId)}/results`, attempt, readResult);
}
