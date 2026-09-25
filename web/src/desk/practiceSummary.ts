/**
 * Read client of `GET /api/practice/summary` (#280) for the study desk's "Repasos para hoy": per
 * topic with practice material, how many reviewed items are due now and how many new ones the
 * daily limit offers, the most due first, plus the totals and the Spanish warnings of the topics
 * the backend could not read. Instants are ISO 8601 strings, as the backend writes them.
 */

import { array, type Decoder, id, int, object, str } from "../protocol/decode";
import { getJson, type ReadResult } from "./api";

function nullable<T>(decode: Decoder<T>): Decoder<T | null> {
  return (value, path) => (value === null ? null : decode(value, path));
}

const count = int({ min: 0 });
const instant = str({ minLength: 1 });

/** `TopicPracticeSummary`: one topic's practice offered now. */
export interface TopicPractice {
  subject_id: string;
  subject_name: string;
  topic_id: string;
  topic_title: string;
  due: number;
  new: number;
  /** The earliest due time after `now` of a reviewed item, or `null`. */
  next_due: string | null;
}

/** `PracticeSummary` of `GET /api/practice/summary`. */
export interface PracticeSummary {
  now: string;
  topics: TopicPractice[];
  totals: { due: number; new: number; topics: number };
  warnings: string[];
}

export const decodePracticeSummary: Decoder<PracticeSummary> = object({
  now: instant,
  topics: array(
    object({
      subject_id: id,
      subject_name: str({ minLength: 1 }),
      topic_id: id,
      topic_title: str({ minLength: 1 }),
      due: count,
      new: count,
      next_due: nullable(instant),
    }),
  ),
  totals: object({ due: count, new: count, topics: count }),
  warnings: array(str()),
});

export function fetchPracticeSummary(): Promise<ReadResult<PracticeSummary>> {
  return getJson("/api/practice/summary", decodePracticeSummary);
}

/** The topics with something to review now (due or new), in the backend's order. */
export function topicsToReview(summary: PracticeSummary): TopicPractice[] {
  return summary.topics.filter((topic) => topic.due > 0 || topic.new > 0);
}

/** The earliest `next_due` of any topic, or `null` when none carries one. */
export function nextDue(summary: PracticeSummary): string | null {
  let earliest: { iso: string; time: number } | null = null;
  for (const topic of summary.topics) {
    if (topic.next_due === null) continue;
    const time = Date.parse(topic.next_due);
    if (Number.isNaN(time)) continue;
    if (earliest === null || time < earliest.time) earliest = { iso: topic.next_due, time };
  }
  return earliest?.iso ?? null;
}

/** "12 pendientes, 5 nuevas", leaving out a zero count ("1 pendiente", "3 nuevas"). */
export function countsText(due: number, fresh: number): string {
  const parts: string[] = [];
  if (due > 0) parts.push(due === 1 ? "1 pendiente" : `${due} pendientes`);
  if (fresh > 0) parts.push(fresh === 1 ? "1 nueva" : `${fresh} nuevas`);
  return parts.length === 0 ? "nada pendiente" : parts.join(", ");
}

const DUE_FORMAT = new Intl.DateTimeFormat("es-ES", { dateStyle: "long", timeStyle: "short" });

/** An ISO instant as "26 de septiembre de 2026, 10:00" (the string itself if unparseable). */
export function formatDue(iso: string): string {
  const time = Date.parse(iso);
  return Number.isNaN(time) ? iso : DUE_FORMAT.format(new Date(time));
}
