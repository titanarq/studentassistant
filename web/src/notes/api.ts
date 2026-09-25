/**
 * Read client of the notes viewer over the web read API (#38): the master notes of a topic,
 * a source's sidecar metadata and text content, and a transcript span. Results use the study
 * desk's `ReadResult` (`describeFailure` puts a failure in one Spanish sentence).
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type Decoder, ProtocolDecodeError } from "../protocol";
import { array, id, int, object, str } from "../protocol/decode";

export interface TopicNotes {
  subject_id: string;
  topic_id: string;
  /** `notes/apuntes.md` as Markdown. */
  text: string;
  /** The notes version, `null` when none is tagged. */
  version: number | null;
}

export interface SourceMeta {
  vault_id: string;
  kind: string;
  media_type: string;
  size: number;
  /** The parsed sidecar, `null` without one. */
  meta: Record<string, unknown> | null;
  /** The sidecar's `transcription` when it carries one as text. */
  transcription: string | null;
}

export interface TranscriptLine {
  seq: number;
  t_start: number;
  t_end: number;
  text: string;
}

export interface TranscriptSpan {
  session_id: string;
  ref: string;
  start_ms: number;
  end_ms: number;
  segments: TranscriptLine[];
}

function nullable<T>(decode: Decoder<T>): Decoder<T | null> {
  return (value, path) => (value === null ? null : decode(value, path));
}

const record: Decoder<Record<string, unknown>> = (value, path) => {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ProtocolDecodeError(path, "expected an object");
  }
  return value as Record<string, unknown>;
};

export const decodeTopicNotes: Decoder<TopicNotes> = object({
  subject_id: id,
  topic_id: id,
  text: str(),
  version: nullable(int({ min: 1 })),
});

export const decodeSourceMeta: Decoder<SourceMeta> = object({
  vault_id: str({ minLength: 1 }),
  kind: str({ minLength: 1 }),
  media_type: str({ minLength: 1 }),
  size: int({ min: 0 }),
  meta: nullable(record),
  transcription: nullable(str()),
});

const ms = int({ min: 0 });

export const decodeTranscriptSpan: Decoder<TranscriptSpan> = (value, path) => {
  const full = object({
    session_id: id,
    subject_id: id,
    topic_id: id,
    ref: str(),
    start_ms: ms,
    end_ms: ms,
    segments: array(object({ seq: int({ min: 0 }), t_start: ms, t_end: ms, text: str() })),
  })(value, path);
  return { session_id: full.session_id, ref: full.ref, start_ms: full.start_ms, end_ms: full.end_ms, segments: full.segments };
};

async function get<T>(path: string, read: (response: Response) => Promise<T>): Promise<ReadResult<T>> {
  let response: Response;
  try {
    response = await fetch(path);
  } catch {
    return { kind: "unreachable" };
  }
  if (response.status === 404) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown } | undefined)?.detail;
    } catch {
      detail = undefined;
    }
    return { kind: "not-found", detail: typeof detail === "string" && detail !== "" ? detail : "No encontrado." };
  }
  if (!response.ok) return { kind: "error", status: response.status };
  try {
    return { kind: "ok", value: await read(response) };
  } catch {
    return { kind: "error", status: response.status };
  }
}

function json<T>(decode: Decoder<T>) {
  return async (response: Response) => decode(await response.json(), "");
}

/** The URL of a source's bytes (`GET /api/sources/{vault_id}`), usable as an `<img src>`. */
export function sourceUrl(vaultId: string): string {
  return `/api/sources/${vaultId.split("/").map(encodeURIComponent).join("/")}`;
}

export function fetchNotes(subjectId: string, topicId: string): Promise<ReadResult<TopicNotes>> {
  return get(`/api${topicPath(subjectId, topicId)}/notes`, json(decodeTopicNotes));
}

export function fetchSourceMeta(vaultId: string): Promise<ReadResult<SourceMeta>> {
  return get(`${sourceUrl(vaultId)}/meta`, json(decodeSourceMeta));
}

/** A text source (a web snapshot, a page transcription, a PDF page's text) as a string. */
export function fetchSourceText(vaultId: string): Promise<ReadResult<string>> {
  return get(sourceUrl(vaultId), (response) => response.text());
}

export function fetchTranscript(
  subjectId: string,
  topicId: string,
  sessionId: string,
  span: string,
): Promise<ReadResult<TranscriptSpan>> {
  const query = new URLSearchParams({ subject: subjectId, topic: topicId, t: span });
  return get(`/api/sessions/${encodeURIComponent(sessionId)}/transcript?${query}`, json(decodeTranscriptSpan));
}
