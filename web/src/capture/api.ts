/**
 * The capture page's REST client (#40): subjects and topics, the session lifecycle and the
 * multipart capture upload (protocol/README.md, the `rest.*` schemas). Request bodies are the
 * bindings' own request types and every answer goes through `parseMessage`, so a body of any
 * other shape is reported instead of quietly used. The page runs on the PC itself, which the
 * backend trusts while it listens on loopback (docs/modules/server.md), so no bearer token is
 * sent or kept here.
 */

import {
  type CaptureUploadRequest,
  type CaptureUploadResponse,
  type MessageTypes,
  parseMessage,
  ProtocolDecodeError,
  type Session,
  type SessionEndRequest,
  type SessionEndResponse,
  type SessionStartRequest,
  type Subject,
  type SubjectCreateRequest,
  type SubjectsListResponse,
  type Topic,
  type TopicCreateRequest,
  type TopicsListResponse,
} from "../protocol";

/** The `multipart/form-data` part carrying the burst's `rest.sessions.captures.request` JSON. */
export const METADATA_PART = "metadata";

export const SUBJECTS_PATH = "/api/subjects";
export const SESSIONS_PATH = "/api/sessions";

export function topicsPath(subjectId: string): string {
  return `${SUBJECTS_PATH}/${encodeURIComponent(subjectId)}/topics`;
}

export function sessionResumePath(sessionId: string): string {
  return `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/resume`;
}

export function sessionEndPath(sessionId: string): string {
  return `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/end`;
}

export function sessionCapturesPath(sessionId: string): string {
  return `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/captures`;
}

/**
 * What every call here answers: the decoded body, or how it failed. `refused` carries the
 * backend's own Spanish `detail` (an unknown topic, a session already open on it, a burst it
 * will not take), which the page shows as it comes; `unexpected` is a 2xx body that is not the
 * protocol message the endpoint promises, and `problem` is the decoder's own wording, meant for
 * a log and never for the student.
 */
export type ApiResult<T> =
  | { kind: "ok"; value: T }
  | { kind: "refused"; status: number; detail: string }
  | { kind: "error"; status: number }
  | { kind: "unexpected"; status: number; expected: string; problem: string }
  | { kind: "unreachable" };

export type SubjectsResult = ApiResult<SubjectsListResponse>;
export type SubjectResult = ApiResult<Subject>;
export type TopicsResult = ApiResult<TopicsListResponse>;
export type TopicResult = ApiResult<Topic>;
export type SessionResult = ApiResult<Session>;
export type SessionEndResult = ApiResult<SessionEndResponse>;
export type CapturesResult = ApiResult<CaptureUploadResponse>;

/** The answers this client decodes, each named by the schema it must match. */
type Answer =
  | "rest.subjects.list.response"
  | "rest.subjects.create.response"
  | "rest.topics.list.response"
  | "rest.topics.create.response"
  | "rest.sessions.start.response"
  | "rest.sessions.resume.response"
  | "rest.sessions.end.response"
  | "rest.sessions.captures.response";

type Body = SubjectCreateRequest | TopicCreateRequest | SessionStartRequest | SessionEndRequest;

function jsonPost(body: Body): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "string" && detail !== "" ? detail : null;
}

async function call<N extends Answer>(
  path: string,
  init: RequestInit,
  expected: N,
): Promise<ApiResult<MessageTypes[N]>> {
  let response: Response;
  try {
    response = await fetch(path, init);
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
    const detail = detailOf(body);
    return detail === null
      ? { kind: "error", status: response.status }
      : { kind: "refused", status: response.status, detail };
  }
  try {
    return { kind: "ok", value: parseMessage(expected, body) };
  } catch (problem) {
    if (problem instanceof ProtocolDecodeError) {
      return {
        kind: "unexpected",
        status: response.status,
        expected,
        problem: problem.message,
      };
    }
    throw problem;
  }
}

/** One still of a burst: the bytes, named by the `images[].part` of the metadata they belong to. */
export interface CapturedImage {
  part: string;
  blob: Blob;
}

/**
 * The burst's body: the metadata JSON plus one part per image, each with the content type its
 * metadata entry declares, which is what the backend checks the part against.
 */
function burstBody(metadata: CaptureUploadRequest, images: readonly CapturedImage[]): FormData {
  const form = new FormData();
  form.append(
    METADATA_PART,
    new Blob([JSON.stringify(metadata)], { type: "application/json" }),
    `${METADATA_PART}.json`,
  );
  for (const image of images) {
    const declared = metadata.images.find((entry) => entry.part === image.part);
    form.append(
      image.part,
      declared === undefined
        ? image.blob
        : new Blob([image.blob], { type: declared.content_type }),
    );
  }
  return form;
}

export async function listSubjects(): Promise<SubjectsResult> {
  return call(SUBJECTS_PATH, { method: "GET" }, "rest.subjects.list.response");
}

export async function createSubject(name: string): Promise<SubjectResult> {
  return call(SUBJECTS_PATH, jsonPost({ name }), "rest.subjects.create.response");
}

export async function listTopics(subjectId: string): Promise<TopicsResult> {
  return call(topicsPath(subjectId), { method: "GET" }, "rest.topics.list.response");
}

export async function createTopic(subjectId: string, name: string): Promise<TopicResult> {
  return call(topicsPath(subjectId), jsonPost({ name }), "rest.topics.create.response");
}

export async function startSession(
  subjectId: string,
  topicId: string,
  clientTimeMs: number,
): Promise<SessionResult> {
  return call(
    SESSIONS_PATH,
    jsonPost({ subject_id: subjectId, topic_id: topicId, client_time_ms: clientTimeMs }),
    "rest.sessions.start.response",
  );
}

export async function resumeSession(sessionId: string): Promise<SessionResult> {
  return call(sessionResumePath(sessionId), { method: "POST" }, "rest.sessions.resume.response");
}

export async function endSession(
  sessionId: string,
  reason: SessionEndRequest["reason"],
  clientTimeMs: number,
): Promise<SessionEndResult> {
  return call(
    sessionEndPath(sessionId),
    jsonPost({ client_time_ms: clientTimeMs, reason }),
    "rest.sessions.end.response",
  );
}

/**
 * Uploads one burst to a session. A `capture_id` the backend already stored comes back as
 * `status: "duplicate"`, which counts as stored, so a retry is safe.
 */
export async function uploadCaptures(
  sessionId: string,
  metadata: CaptureUploadRequest,
  images: readonly CapturedImage[],
): Promise<CapturesResult> {
  return call(
    sessionCapturesPath(sessionId),
    { method: "POST", body: burstBody(metadata, images) },
    "rest.sessions.captures.response",
  );
}
