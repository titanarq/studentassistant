// Bodies of the REST endpoints under `/api` (protocol/README.md, `rest.*` schemas).
// Times are Unix epoch ms: `client_time_ms` on the client's clock, `*_at_ms` and
// `server_time_ms` on the backend's.

import {
  array,
  bool,
  captureId,
  type Decoder,
  epochMs,
  id,
  int,
  literal,
  name,
  object,
  str,
} from "./decode";
import { VERSION_PATTERN } from "./version";

export const protocolVersion = str({ pattern: VERSION_PATTERN });

/** The longest `Topic.digest_excerpt` (since 1.3). */
export const DIGEST_EXCERPT_MAX = 400;

// POST /api/pair

/** Exchanges the one-time code shown in the pairing QR for a long-lived bearer token. */
export interface PairRequest {
  pairing_code: string;
  device_name: string;
  client_kind: "web" | "android";
  protocol_version: string;
}

export interface PairResponse {
  device_id: string;
  /** Sent as `Authorization: Bearer <token>`; never logged. */
  token: string;
  protocol_version: string;
}

// GET /api/health

export interface HealthResponse {
  status: "ok";
  protocol_version: string;
  server_time_ms: number;
}

// GET/POST /api/subjects

export interface Subject {
  subject_id: string;
  name: string;
}

export interface SubjectsListResponse {
  subjects: Subject[];
}

export interface SubjectCreateRequest {
  name: string;
}

// GET/POST /api/subjects/{subject_id}/topics

export interface Topic {
  topic_id: string;
  subject_id: string;
  name: string;
  /** The session still open on this topic, if any; the client resumes it. */
  open_session_id?: string;
  /** Since 1.1: start of the topic's latest session, backend clock, epoch ms; absent when unknown. */
  last_session_at_ms?: number;
  /** Since 1.1: open pending-review items (doubts awaiting the student); absent when unknown. */
  pending_count?: number;
  /** Since 1.3: the topic digest's summary paragraph (where the topic was left); absent when unknown. */
  digest_excerpt?: string;
}

export interface TopicsListResponse {
  subject_id: string;
  topics: Topic[];
}

export interface TopicCreateRequest {
  name: string;
}

// POST /api/sessions, /api/sessions/{id}/resume, /api/sessions/{id}/end

export interface SessionStartRequest {
  subject_id: string;
  topic_id: string;
  client_time_ms: number;
}

/** An open session, returned by start and resume. */
export interface Session {
  session_id: string;
  subject_id: string;
  topic_id: string;
  status: "active";
  started_at_ms: number;
  /** Always `/ws/sessions/{session_id}`. */
  ws_path: string;
  protocol_version: string;
  /** Captures the backend already stored, so a resuming client re-uploads only the rest. */
  received_capture_ids?: string[];
}

export interface SessionEndRequest {
  client_time_ms: number;
  reason: "button" | "command";
}

export interface SessionEndResponse {
  session_id: string;
  status: "ended";
  ended_at_ms: number;
}

// POST /api/sessions/{id}/captures (multipart/form-data)

export interface CaptureImage {
  /** Name of the multipart part carrying this image's bytes, `image_<n>`. */
  part: string;
  content_type: "image/jpeg" | "image/png" | "image/webp";
  width_px: number;
  height_px: number;
  client_time_ms: number;
}

/** The `metadata` JSON part of a capture burst; the images travel as sibling parts. */
export interface CaptureUploadRequest {
  capture_id: string;
  trigger: "button" | "command";
  /** The `command` that asked for this capture, when `trigger` is `command`. */
  command_id?: string;
  client_time_ms: number;
  images: CaptureImage[];
}

export interface CaptureUploadResponse {
  capture_id: string;
  session_id: string;
  status: "stored" | "duplicate";
  image_count: number;
  received_at_ms: number;
}

// GET /api/search

/**
 * One match of a search over the vault. `path` is the vault-relative file the text is in;
 * `source` the source it belongs to (absent for notes and transcripts). A transcript hit carries
 * its `session`, the segment's `seq` and its `t_start` in session ms. `snippet` marks each
 * matched term between U+0002 and U+0003.
 */
export interface SearchHit {
  kind: "notes" | "page" | "pdf" | "web" | "transcript";
  path: string;
  source?: string;
  subject: string;
  topic: string;
  session?: string;
  seq?: number;
  t_start?: number;
  snippet: string;
}

/** The best matches of `query`, best first. */
export interface SearchResponse {
  query: string;
  hits: SearchHit[];
}

// POST /api/subjects/{subject_id}/topics/{topic_id}/web-pages

/** An http(s) address, as `rest.topics.web_pages.create.request` accepts it. */
export const WEB_PAGE_URL_PATTERN = /^https?:\/\/\S+$/i;
export const WEB_PAGE_URL_MAX = 2000;

/**
 * A web page the student gives by its address, stored as a source of the topic. `via`: pasted in
 * the web UI (`url`, the default) or shared to the phone app (`share`).
 */
export interface WebPageAddRequest {
  url: string;
  via?: "url" | "share";
}

/**
 * The stored snapshot: `source_id` (`sources/web/NNN-<slug>.md`), `vault_id` (vault-relative
 * path), `title`, the fetched `url`, and `already_kept` when the topic had that page already.
 */
export interface WebPageAddResponse {
  source_id: string;
  vault_id: string;
  title: string;
  url: string;
  already_kept: boolean;
}

// Decoders

export const decodePairRequest: Decoder<PairRequest> = object({
  pairing_code: str({ minLength: 1 }),
  device_name: name,
  client_kind: literal("web", "android"),
  protocol_version: protocolVersion,
});

export const decodePairResponse: Decoder<PairResponse> = object({
  device_id: id,
  token: str({ minLength: 1 }),
  protocol_version: protocolVersion,
});

export const decodeHealthResponse: Decoder<HealthResponse> = object({
  status: literal("ok"),
  protocol_version: protocolVersion,
  server_time_ms: epochMs,
});

export const decodeSubject: Decoder<Subject> = object({ subject_id: id, name });

export const decodeSubjectsListResponse: Decoder<SubjectsListResponse> = object({
  subjects: array(decodeSubject),
});

export const decodeSubjectCreateRequest: Decoder<SubjectCreateRequest> = object({ name });

export const decodeTopic: Decoder<Topic> = object(
  { topic_id: id, subject_id: id, name },
  {
    open_session_id: id,
    last_session_at_ms: epochMs,
    pending_count: int({ min: 0 }),
    digest_excerpt: str({ minLength: 1, maxLength: DIGEST_EXCERPT_MAX }),
  },
);

export const decodeTopicsListResponse: Decoder<TopicsListResponse> = object({
  subject_id: id,
  topics: array(decodeTopic),
});

export const decodeTopicCreateRequest: Decoder<TopicCreateRequest> = object({ name });

export const decodeSessionStartRequest: Decoder<SessionStartRequest> = object({
  subject_id: id,
  topic_id: id,
  client_time_ms: epochMs,
});

export const decodeSession: Decoder<Session> = object(
  {
    session_id: id,
    subject_id: id,
    topic_id: id,
    status: literal("active"),
    started_at_ms: epochMs,
    ws_path: str({ pattern: /^\/ws\/sessions\/[A-Za-z0-9][A-Za-z0-9_-]*$/ }),
    protocol_version: protocolVersion,
  },
  { received_capture_ids: array(captureId) },
);

export const decodeSessionEndRequest: Decoder<SessionEndRequest> = object({
  client_time_ms: epochMs,
  reason: literal("button", "command"),
});

export const decodeSessionEndResponse: Decoder<SessionEndResponse> = object({
  session_id: id,
  status: literal("ended"),
  ended_at_ms: epochMs,
});

export const decodeCaptureImage: Decoder<CaptureImage> = object({
  part: str({ pattern: /^image_[0-9]+$/ }),
  content_type: literal("image/jpeg", "image/png", "image/webp"),
  width_px: int({ min: 1 }),
  height_px: int({ min: 1 }),
  client_time_ms: epochMs,
});

export const decodeCaptureUploadRequest: Decoder<CaptureUploadRequest> = object(
  {
    capture_id: captureId,
    trigger: literal("button", "command"),
    client_time_ms: epochMs,
    images: array(decodeCaptureImage, { minItems: 1 }),
  },
  { command_id: id },
);

export const decodeCaptureUploadResponse: Decoder<CaptureUploadResponse> = object({
  capture_id: captureId,
  session_id: id,
  status: literal("stored", "duplicate"),
  image_count: int({ min: 1 }),
  received_at_ms: epochMs,
});

export const decodeSearchHit: Decoder<SearchHit> = object(
  {
    kind: literal("notes", "page", "pdf", "web", "transcript"),
    path: str({ minLength: 1 }),
    subject: id,
    topic: id,
    snippet: str(),
  },
  { source: str({ minLength: 1 }), session: id, seq: int({ min: 0 }), t_start: int({ min: 0 }) },
);

export const decodeSearchResponse: Decoder<SearchResponse> = object({
  query: str(),
  hits: array(decodeSearchHit),
});

export const decodeWebPageAddRequest: Decoder<WebPageAddRequest> = object(
  { url: str({ pattern: WEB_PAGE_URL_PATTERN, maxLength: WEB_PAGE_URL_MAX }) },
  { via: literal("url", "share") },
);

export const decodeWebPageAddResponse: Decoder<WebPageAddResponse> = object({
  source_id: str({ minLength: 1 }),
  vault_id: str({ minLength: 1 }),
  title: str(),
  url: str({ minLength: 1 }),
  already_kept: bool(),
});
