/**
 * Read-only client of what Claude calls cost (docs/VISION.md §5.13): the cost-cap status of
 * `GET /api/cost` (today's spend and, with a session, the session's, against the configured caps)
 * for the study desk, and a topic's spend per session of `GET /api/subjects/{s}/topics/{t}/cost`
 * for the topic page. Nothing is priced here: the amounts are the backend's, in USD.
 */

import { array, bool, type Decoder, id, int, num, object, str } from "../protocol/decode";
import { getJson, type ReadResult, topicPath } from "./api";

function nullable<T>(decode: Decoder<T>): Decoder<T | null> {
  return (value, path) => (value === null ? null : decode(value, path));
}

const count = int({ min: 0 });
const usd = num({ min: 0 });

/** `CostStatus` of `GET /api/cost`; a `null` cap means no cap is configured. */
export interface CostStatus {
  session_usd: number;
  day_usd: number;
  max_usd_per_session: number | null;
  max_usd_per_day: number | null;
  observer_paused: boolean;
  editor_needs_confirmation: boolean;
  unpriced_session_calls: number;
  unpriced_day_calls: number;
  unpriced_models: string[];
}

export const decodeCostStatus: Decoder<CostStatus> = object({
  session_usd: usd,
  day_usd: usd,
  max_usd_per_session: nullable(usd),
  max_usd_per_day: nullable(usd),
  observer_paused: bool(),
  editor_needs_confirmation: bool(),
  unpriced_session_calls: count,
  unpriced_day_calls: count,
  unpriced_models: array(str({ minLength: 1 })),
});

/** The summed ledger entries of one scope; `usd` leaves out the `unpriced_calls`. */
export interface CostTotals {
  usd: number;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  calls: number;
  unpriced_calls: number;
}

export interface SessionCost extends CostTotals {
  session_id: string;
  /** Epoch ms; `null` when the backend knows no start for the session. */
  started_at_ms: number | null;
}

/** `TopicCost` of `GET /api/subjects/{s}/topics/{t}/cost`. */
export interface TopicCost {
  subject_id: string;
  topic_id: string;
  total: CostTotals;
  sessions: SessionCost[];
  /** The calls bound to no session: the editor, the generators. */
  no_session: CostTotals;
}

const totalsFields = {
  usd,
  tokens: count,
  input_tokens: count,
  output_tokens: count,
  cache_read_tokens: count,
  cache_write_tokens: count,
  calls: count,
  unpriced_calls: count,
};

export const decodeTopicCost: Decoder<TopicCost> = object({
  subject_id: id,
  topic_id: id,
  total: object(totalsFields),
  sessions: array(
    object({ ...totalsFields, session_id: str({ minLength: 1 }), started_at_ms: nullable(int({ min: 0 })) }),
  ),
  no_session: object(totalsFields),
});

/** The session whose spend `GET /api/cost` also reports, when one is active. */
export interface ActiveSession {
  subjectId: string;
  topicId: string;
  sessionId: string;
}

export function fetchCostStatus(session?: ActiveSession): Promise<ReadResult<CostStatus>> {
  const query =
    session === undefined
      ? ""
      : `?${new URLSearchParams({ subject: session.subjectId, topic: session.topicId, session: session.sessionId })}`;
  return getJson(`/api/cost${query}`, decodeCostStatus);
}

export function fetchTopicCost(subjectId: string, topicId: string): Promise<ReadResult<TopicCost>> {
  return getJson(`/api${topicPath(subjectId, topicId)}/cost`, decodeTopicCost);
}

/** An amount of USD as the web shows it: "0,1234 USD" (four decimals: calls cost cents). */
export function formatUsd(amount: number): string {
  return `${amount.toLocaleString("es-ES", { minimumFractionDigits: 4, maximumFractionDigits: 4 })} USD`;
}

/** A token count with Spanish digit grouping: "12.345 tokens". */
export function formatTokens(tokens: number): string {
  return `${tokens.toLocaleString("es-ES", { useGrouping: true })} tokens`;
}
