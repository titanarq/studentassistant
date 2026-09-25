/**
 * Read-only client of the study desk: the subjects and topics of the protocol (`GET
 * /api/subjects`, `GET /api/subjects/{s}/topics`, decoded strictly as protocol `rest.*` bodies)
 * and the web read API's topic card (`GET /api/subjects/{s}/topics/{t}/summary`, #38).
 *
 * The web app declares no protocol version on REST: on the PC it is served by the backend's own
 * build, which answers it as its own version (1.1 or later), so the topic list carries
 * `last_session_at_ms` and `pending_count` when the backend knows them. Both stay optional here,
 * as in the protocol, and a topic without them is shown without them.
 */

import {
  type Decoder,
  decodeSubjectsListResponse,
  decodeTopicsListResponse,
  type Subject,
  type Topic,
} from "../protocol";
import { array, id, int, num, object, str } from "../protocol/decode";

export type ReadResult<T> =
  | { kind: "ok"; value: T }
  /** 404 with the backend's Spanish `detail` (unknown subject or topic). */
  | { kind: "not-found"; detail: string }
  /** Any other non-2xx status, or a body that does not decode. */
  | { kind: "error"; status: number }
  /** The request never got an answer (backend down, network error). */
  | { kind: "unreachable" };

/** Sources of a topic by kind, as `SourceCounts` of the read API. */
export interface SourceCounts {
  notes: number;
  book: number;
  pdf: number;
  web: number;
}

/** `TopicSummary` of `GET /api/subjects/{s}/topics/{t}/summary`. */
export interface TopicSummary {
  subject_id: string;
  topic_id: string;
  sources: SourceCounts;
  sessions: number;
  session_minutes: number;
  open_pending: number;
  notes_version: number | null;
  /** Vault-relative paths of the files under the topic's `generated/`. */
  generated: string[];
}

function nullable<T>(decode: Decoder<T>): Decoder<T | null> {
  return (value, path) => (value === null ? null : decode(value, path));
}

const count = int({ min: 0 });

export const decodeTopicSummary: Decoder<TopicSummary> = object({
  subject_id: id,
  topic_id: id,
  sources: object({ notes: count, book: count, pdf: count, web: count }),
  sessions: count,
  session_minutes: num({ min: 0 }),
  open_pending: count,
  notes_version: nullable(int({ min: 1 })),
  generated: array(str({ minLength: 1 })),
});

const segment = encodeURIComponent;

export function topicPath(subjectId: string, topicId: string): string {
  return `/subjects/${segment(subjectId)}/topics/${segment(topicId)}`;
}

export async function getJson<T>(path: string, decode: Decoder<T>): Promise<ReadResult<T>> {
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
  if (response.status === 404) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    return { kind: "not-found", detail: typeof detail === "string" && detail !== "" ? detail : "No encontrado." };
  }
  if (!response.ok) return { kind: "error", status: response.status };
  try {
    return { kind: "ok", value: decode(body, "") };
  } catch {
    return { kind: "error", status: response.status };
  }
}

export async function fetchSubjects(): Promise<ReadResult<Subject[]>> {
  const result = await getJson("/api/subjects", decodeSubjectsListResponse);
  return result.kind === "ok" ? { kind: "ok", value: result.value.subjects } : result;
}

export async function fetchTopics(subjectId: string): Promise<ReadResult<Topic[]>> {
  const result = await getJson(`/api/subjects/${segment(subjectId)}/topics`, decodeTopicsListResponse);
  return result.kind === "ok" ? { kind: "ok", value: result.value.topics } : result;
}

export function fetchTopicSummary(subjectId: string, topicId: string): Promise<ReadResult<TopicSummary>> {
  return getJson(`/api${topicPath(subjectId, topicId)}/summary`, decodeTopicSummary);
}

/** A failed read as one Spanish sentence. */
export function describeFailure(result: Exclude<ReadResult<unknown>, { kind: "ok" }>): string {
  switch (result.kind) {
    case "not-found":
      return result.detail;
    case "error":
      return `El servidor respondió con un error (${result.status}).`;
    case "unreachable":
      return "No se pudo conectar con el servidor.";
  }
}

/** A backend epoch-ms instant as a Spanish date, "24 de septiembre de 2026". */
export function formatDate(epochMs: number): string {
  return new Date(epochMs).toLocaleDateString("es-ES", { day: "numeric", month: "long", year: "numeric" });
}
