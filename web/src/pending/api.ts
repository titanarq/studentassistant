/**
 * Read client of the pending-doubts panel: `GET /api/subjects/{s}/topics/{t}/pending?status=`
 * (#55), the observer's pending-review queue of a topic, decoded strictly as the server's
 * `TopicPending`. Results use the study desk's `ReadResult`.
 *
 * `kind` and `status` are kept as strings (with Spanish labels for the known ones) so a kind or
 * status added later is shown under a generic label instead of failing the whole panel.
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type Decoder, ProtocolDecodeError } from "../protocol";
import { array, int, object, str } from "../protocol/decode";

export type PendingFilter = "open" | "closed" | "all";

export interface PendingRefs {
  /** Page capture ids. */
  pages: string[];
  /** Transcript segment ids. */
  segments: string[];
  /** Source ids. */
  sources: string[];
}

export interface EventRef {
  session_id: string;
  seq: number;
}

export interface PendingItem {
  id: string;
  /** `illegible`, `unexplained_concept`, `incomplete`, `possible_error`, `contradiction`. */
  kind: string;
  /** What the doubt is, in Spanish. */
  text: string;
  refs: PendingRefs;
  created_by: string;
  /** `open`, `auto_resolved`, `resolved` or `dismissed`. */
  status: string;
  resolution: string | null;
  added_at: EventRef;
  resolved_at: EventRef | null;
  merged_ids: string[];
}

export interface TopicPending {
  subject_id: string;
  topic_id: string;
  /** Open items of the whole queue, whatever the filter. */
  open_count: number;
  /** The items the filter keeps: open ones first, each group in the order added. */
  items: PendingItem[];
}

/** Spanish label of each known kind, in the order the panel groups them. */
export const KIND_LABELS: Record<string, string> = {
  contradiction: "Contradicción",
  possible_error: "Posible error",
  illegible: "Ilegible",
  incomplete: "Incompleto",
  unexplained_concept: "Concepto sin explicar",
};

export const STATUS_LABELS: Record<string, string> = {
  open: "Abierta",
  auto_resolved: "Resuelta con las fuentes",
  resolved: "Resuelta",
  dismissed: "Descartada",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? "Otra duda";
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function nullable<T>(decode: Decoder<T>): Decoder<T | null> {
  return (value, path) => (value === null ? null : decode(value, path));
}

const nonEmpty = str({ minLength: 1 });
const ids = array(nonEmpty);
const eventRef: Decoder<EventRef> = object({ session_id: nonEmpty, seq: int({ min: 1 }) });

const decodeRefs: Decoder<PendingRefs> = (value, path) => {
  const refs = object({}, { pages: ids, segments: ids, sources: ids })(value, path);
  return { pages: refs.pages ?? [], segments: refs.segments ?? [], sources: refs.sources ?? [] };
};

export const decodePendingItem: Decoder<PendingItem> = object({
  id: nonEmpty,
  kind: nonEmpty,
  text: str(),
  refs: decodeRefs,
  created_by: nonEmpty,
  status: nonEmpty,
  resolution: nullable(str()),
  added_at: eventRef,
  resolved_at: nullable(eventRef),
  merged_ids: ids,
});

export const decodeTopicPending: Decoder<TopicPending> = object({
  subject_id: nonEmpty,
  topic_id: nonEmpty,
  open_count: int({ min: 0 }),
  items: array(decodePendingItem),
});

export function pendingPath(subjectId: string, topicId: string, filter: PendingFilter): string {
  return `/api${topicPath(subjectId, topicId)}/pending?status=${filter}`;
}

export async function fetchPending(
  subjectId: string,
  topicId: string,
  filter: PendingFilter,
): Promise<ReadResult<TopicPending>> {
  let response: Response;
  try {
    response = await fetch(pendingPath(subjectId, topicId, filter));
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
    return { kind: "ok", value: decodeTopicPending(body, "") };
  } catch (error) {
    if (error instanceof ProtocolDecodeError) return { kind: "error", status: response.status };
    throw error;
  }
}
