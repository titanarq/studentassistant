/**
 * Client of the editor's doubts API (#68, `docs/modules/server.md`), the write side of the
 * pending-doubts panel:
 * - `GET /api/subjects/{s}/topics/{t}/doubts` -> `DoubtsQueue`: every doubt of the topic, open ones
 *   first, each with the editor's latest question (suggested answers, source options for a
 *   contradiction) and, once closed, its outcome. Decoded strictly, read as a `ReadResult`.
 * - `POST .../doubts/review`: the editor auto-resolves what the sources answer and writes a
 *   question for every other open doubt. The web calls it after "prepárame el tema" and when the
 *   student asks for the questions of doubts that have none.
 * - `POST .../doubts/{id}/answer` (a suggestion by number, free text, or the source that is right
 *   in a contradiction) and `POST .../doubts/{id}/dismiss`.
 *
 * The writes answer an `ActionResult`: a refusal keeps the backend's Spanish `detail` (a closed
 * doubt, an unended session, another operation running, a reached cost cap are 409), and
 * `overCap` marks the one refusal a retry with `confirm_over_cap` can get past.
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type Decoder, ProtocolDecodeError } from "../protocol";
import { array, int, object, str } from "../protocol/decode";
import { decodePendingItem, eventRef, type EventRef, nullable, type PendingItem } from "./api";

export interface SourceOption {
  source_id: string;
  /** What that source says. */
  says: string;
}

export interface Evidence {
  source_id: string;
  quote: string;
}

export interface DoubtQuestion {
  pending_id: string;
  /** The editor's question, in Spanish. */
  question: string;
  /** 1-3 likely answers; the student picks one by number (from 1). */
  suggestions: string[];
  /** For a contradiction: what each source says; the student picks the one that is right. */
  options: SourceOption[];
  asked_at: EventRef | null;
}

export interface DoubtOutcome {
  pending_id: string;
  /** `resolved`, `auto_resolved` or `dismissed`. */
  status: string;
  resolution: string | null;
  evidence: Evidence[];
  answer: string | null;
  suggestion: string | null;
  chosen_source: SourceOption | null;
  discarded: SourceOption[];
  keep_discarded: boolean;
  notes_changed: boolean;
  warning: string | null;
  resolved_at: EventRef | null;
}

export interface Doubt {
  item: PendingItem;
  question: DoubtQuestion | null;
  outcome: DoubtOutcome | null;
}

export interface DoubtsQueue {
  subject: string;
  topic: string;
  open_count: number;
  /** The open doubt to ask next, `null` when none is open. */
  current: string | null;
  /** Open doubts first. */
  items: Doubt[];
}

/** The student's answer: exactly what `POST .../answer` takes (besides `confirm_over_cap`). */
export interface DoubtAnswer {
  /** 1-based, into the question's `suggestions`. */
  suggestion?: number;
  /** Free text, alone or as a comment on a suggestion or a source. */
  answer?: string;
  /** For a contradiction: the option whose source is right. */
  source_id?: string;
  /** With `source_id`: keep a note of what the other sources say. */
  keep_discarded?: boolean;
}

export interface ReviewResult {
  auto_resolved: string[];
  asked: string[];
  notes_changed: boolean;
  warning: string | null;
}

export interface ResolutionResult {
  pending_id: string;
  /** `resolved` or `dismissed`. */
  status: string;
  resolution: string | null;
  notes_changed: boolean;
  warning: string | null;
}

export type ActionResult<T> =
  | { kind: "ok"; value: T }
  /** A non-2xx answer with a Spanish `detail`; `overCap` when confirming would get past it. */
  | { kind: "refused"; status: number; detail: string; overCap: boolean }
  /** Any other non-2xx status, or a 2xx body that is not what was asked. */
  | { kind: "error"; status: number }
  /** The request never got an answer (backend down, network error). */
  | { kind: "unreachable" };

const nonEmpty = str({ minLength: 1 });
const bool: Decoder<boolean> = (value, path) => {
  if (typeof value !== "boolean") throw new ProtocolDecodeError(path, "expected a boolean");
  return value;
};

const sourceOption: Decoder<SourceOption> = object({ source_id: nonEmpty, says: str() });

export const decodeDoubtQuestion: Decoder<DoubtQuestion> = object({
  pending_id: nonEmpty,
  question: str(),
  suggestions: array(str()),
  options: array(sourceOption),
  asked_at: nullable(eventRef),
});

export const decodeDoubtOutcome: Decoder<DoubtOutcome> = object({
  pending_id: nonEmpty,
  status: nonEmpty,
  resolution: nullable(str()),
  evidence: array(object({ source_id: nonEmpty, quote: str() })),
  answer: nullable(str()),
  suggestion: nullable(str()),
  chosen_source: nullable(sourceOption),
  discarded: array(sourceOption),
  keep_discarded: bool,
  notes_changed: bool,
  warning: nullable(str()),
  resolved_at: nullable(eventRef),
});

export const decodeDoubtsQueue: Decoder<DoubtsQueue> = object({
  subject: nonEmpty,
  topic: nonEmpty,
  open_count: int({ min: 0 }),
  current: nullable(nonEmpty),
  items: array(
    object({
      item: decodePendingItem,
      question: nullable(decodeDoubtQuestion),
      outcome: nullable(decodeDoubtOutcome),
    }),
  ),
});

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** Lenient: only what the panel shows, so a field added to the result does not fail an answer. */
function readReview(body: unknown): ReviewResult | null {
  if (typeof body !== "object" || body === null) return null;
  const b = body as Record<string, unknown>;
  if (!Array.isArray(b.asked) || !Array.isArray(b.auto_resolved)) return null;
  return {
    auto_resolved: strings(b.auto_resolved),
    asked: strings(b.asked),
    notes_changed: b.notes_changed === true,
    warning: optionalString(b.warning),
  };
}

function readResolution(body: unknown): ResolutionResult | null {
  if (typeof body !== "object" || body === null) return null;
  const b = body as Record<string, unknown>;
  if (typeof b.pending_id !== "string" || typeof b.status !== "string") return null;
  return {
    pending_id: b.pending_id,
    status: b.status,
    resolution: optionalString(b.resolution),
    notes_changed: b.notes_changed === true,
    warning: optionalString(b.warning),
  };
}

/** What a review did, in one Spanish sentence. */
export function describeReview(result: ReviewResult): string {
  const solved = result.auto_resolved.length;
  const asked = result.asked.length;
  if (solved === 0 && asked === 0) return "No había dudas abiertas que revisar.";
  const parts: string[] = [];
  if (solved > 0) parts.push(solved === 1 ? "ha resuelto 1 duda con tus fuentes" : `ha resuelto ${solved} dudas con tus fuentes`);
  if (asked > 0) parts.push(asked === 1 ? "tiene 1 pregunta para ti" : `tiene ${asked} preguntas para ti`);
  return `El editor ${parts.join(" y ")}.`;
}

/** A reached cost cap: the one 409 a retry with `confirm_over_cap` gets past. */
export function isOverCap(status: number, detail: string): boolean {
  return status === 409 && detail.startsWith("Se ha alcanzado el límite de gasto");
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/** `POST path` with an optional JSON body; `read` turns a 2xx body into the result or `null`. */
export async function postAction<T>(
  path: string,
  body: unknown,
  read: (body: unknown) => T | null,
): Promise<ActionResult<T>> {
  let response: Response;
  try {
    response = await fetch(
      path,
      body === undefined
        ? { method: "POST" }
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    );
  } catch {
    return { kind: "unreachable" };
  }
  const json = await readJson(response);
  if (!response.ok) {
    const detail = (json as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail !== "") {
      return { kind: "refused", status: response.status, detail, overCap: isOverCap(response.status, detail) };
    }
    return { kind: "error", status: response.status };
  }
  const value = read(json);
  return value === null ? { kind: "error", status: response.status } : { kind: "ok", value };
}

/** A failed write as one Spanish sentence. */
export function describeActionFailure(result: Exclude<ActionResult<unknown>, { kind: "ok" }>): string {
  switch (result.kind) {
    case "refused":
      return result.detail;
    case "error":
      return `El servidor respondió con un error (${result.status}).`;
    case "unreachable":
      return "No se pudo conectar con el servidor.";
  }
}

export function doubtsPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/doubts`;
}

function doubtPath(subjectId: string, topicId: string, pendingId: string, action: "answer" | "dismiss"): string {
  return `${doubtsPath(subjectId, topicId)}/${encodeURIComponent(pendingId)}/${action}`;
}

export async function fetchDoubts(subjectId: string, topicId: string): Promise<ReadResult<DoubtsQueue>> {
  let response: Response;
  try {
    response = await fetch(doubtsPath(subjectId, topicId));
  } catch {
    return { kind: "unreachable" };
  }
  const body = await readJson(response);
  if (response.status === 404) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    return { kind: "not-found", detail: typeof detail === "string" && detail !== "" ? detail : "No encontrado." };
  }
  if (!response.ok) return { kind: "error", status: response.status };
  try {
    return { kind: "ok", value: decodeDoubtsQueue(body, "") };
  } catch (error) {
    if (error instanceof ProtocolDecodeError) return { kind: "error", status: response.status };
    throw error;
  }
}

export function reviewDoubts(
  subjectId: string,
  topicId: string,
  confirmOverCap = false,
): Promise<ActionResult<ReviewResult>> {
  return postAction(`${doubtsPath(subjectId, topicId)}/review`, { confirm_over_cap: confirmOverCap }, readReview);
}

export function answerDoubt(
  subjectId: string,
  topicId: string,
  pendingId: string,
  answer: DoubtAnswer,
  confirmOverCap = false,
): Promise<ActionResult<ResolutionResult>> {
  return postAction(
    doubtPath(subjectId, topicId, pendingId, "answer"),
    { ...answer, confirm_over_cap: confirmOverCap },
    readResolution,
  );
}

export function dismissDoubt(
  subjectId: string,
  topicId: string,
  pendingId: string,
): Promise<ActionResult<ResolutionResult>> {
  return postAction(doubtPath(subjectId, topicId, pendingId, "dismiss"), undefined, readResolution);
}
