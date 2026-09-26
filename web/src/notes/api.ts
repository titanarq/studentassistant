/**
 * Read client of the notes viewer over the web read API (#38): the master notes of a topic,
 * a source's sidecar metadata and text content, and a transcript span. Results use the study
 * desk's `ReadResult` (`describeFailure` puts a failure in one Spanish sentence). Also the
 * student's edits (#316): saving the whole notes and uploading a pasted image.
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type Decoder, ProtocolDecodeError } from "../protocol";
import { array, id, int, object, str } from "../protocol/decode";
import { errorCode } from "../protocol/errors";

export interface TopicNotes {
  subject_id: string;
  topic_id: string;
  /** `notes/apuntes.md` as Markdown. */
  text: string;
  /** The notes version, `null` when none is tagged. */
  version: number | null;
  /**
   * SHA-256 hex of `text` (epic #311, #313): the token a student save sends back as
   * `base_revision`. Absent from a backend that does not send it yet.
   */
  revision?: string;
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
}, { revision: str({ minLength: 1 }) });

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

// -- the student's edits (#316, over #313) -------------------------------------------------------

/** What a student save (`PUT .../notes`) came to. */
export type SaveNotesResult =
  /** Saved: the notes as stored (normalised) and their new revision. */
  | { kind: "saved"; text: string; revision: string; changedSections: string[]; notesChanged: boolean }
  /** `409 notes_changed`: the notes changed since the edit began; the current ones. */
  | { kind: "changed"; text: string; revision: string | null }
  /** `409 notes_busy`: "prepárame el tema" or another rewrite holds the notes. */
  | { kind: "busy" }
  /** `422`: the text breaks the notes format; the Spanish errors. */
  | { kind: "invalid"; errors: string[] }
  | { kind: "failed"; message: string };

export const UNREACHABLE = "No se pudo conectar con el servidor.";
export const INVALID_REQUEST = "El servidor no aceptó los apuntes (quizá son demasiado largos).";

async function body(response: Response): Promise<Record<string, unknown>> {
  try {
    const value: unknown = await response.json();
    return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function detailOf(value: Record<string, unknown>, fallback: string): string {
  return typeof value.detail === "string" && value.detail !== "" ? value.detail : fallback;
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item !== "") : [];

/**
 * Saves the whole `apuntes.md` the student ended up with (`PUT .../notes`, epic #311):
 * `baseRevision` is the `revision` of the notes the edit started from (`null`: there were none).
 */
export async function saveNotes(
  subjectId: string,
  topicId: string,
  text: string,
  baseRevision: string | null,
): Promise<SaveNotesResult> {
  let response: Response;
  try {
    response = await fetch(`/api${topicPath(subjectId, topicId)}/notes`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, base_revision: baseRevision }),
    });
  } catch {
    return { kind: "failed", message: UNREACHABLE };
  }
  const value = await body(response);
  if (response.ok) {
    if (typeof value.notes !== "string" || typeof value.revision !== "string") {
      return { kind: "failed", message: `El servidor respondió con un error (${response.status}).` };
    }
    return {
      kind: "saved",
      text: value.notes,
      revision: value.revision,
      changedSections: strings(value.changed_sections),
      notesChanged: value.notes_changed !== false,
    };
  }
  if (response.status === 409) {
    const code = errorCode(value);
    // Without a code (an older pairing), the current notes in the body still tell a stale base.
    if (code === "notes_changed" || (code === null && typeof value.text === "string")) {
      return {
        kind: "changed",
        text: typeof value.text === "string" ? value.text : "",
        revision: typeof value.revision === "string" && value.revision !== "" ? value.revision : null,
      };
    }
    if (code === "notes_busy" || code === null) return { kind: "busy" };
    return { kind: "failed", message: detailOf(value, `El servidor respondió con un error (${response.status}).`) };
  }
  if (response.status === 422) {
    const errors = strings(value.errors);
    if (errors.length > 0) return { kind: "invalid", errors };
    return { kind: "invalid", errors: [typeof value.detail === "string" && value.detail !== "" ? value.detail : INVALID_REQUEST] };
  }
  return { kind: "failed", message: detailOf(value, `El servidor respondió con un error (${response.status}).`) };
}

/** A pasted image stored as a source of the topic (`POST .../sources/images`). */
export type UploadImageResult =
  | { kind: "ok"; sourceId: string; markdown: string }
  | { kind: "failed"; message: string };

export async function uploadPastedImage(subjectId: string, topicId: string, file: Blob): Promise<UploadImageResult> {
  const form = new FormData();
  form.append("file", file, file instanceof File && file.name !== "" ? file.name : "imagen");
  let response: Response;
  try {
    response = await fetch(`/api${topicPath(subjectId, topicId)}/sources/images`, { method: "POST", body: form });
  } catch {
    return { kind: "failed", message: UNREACHABLE };
  }
  const value = await body(response);
  if (response.ok && typeof value.markdown === "string" && value.markdown !== "") {
    return { kind: "ok", sourceId: typeof value.source_id === "string" ? value.source_id : "", markdown: value.markdown };
  }
  return { kind: "failed", message: detailOf(value, `El servidor respondió con un error (${response.status}).`) };
}

/**
 * The URL an image of the notes is shown from: a link to a topic source (`../sources/<kind>/<file>`)
 * goes through the read API; anything else is not shown (`null`).
 */
export function notesImageUrl(subjectId: string, topicId: string, src: string): string | null {
  const match = /^\.\.\/sources\/([a-z]+)\/([a-z0-9][a-z0-9._-]*)$/.exec(src);
  return match ? sourceUrl(`subjects/${subjectId}/topics/${topicId}/sources/${match[1]}/${match[2]}`) : null;
}
