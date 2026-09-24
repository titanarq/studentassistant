// Bodies of the REST endpoints under `/api` (protocol/README.md, `rest.*` schemas).
// Times are Unix epoch ms: `client_time_ms` on the client's clock, `*_at_ms` and
// `server_time_ms` on the backend's.

import {
  array,
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
  { open_session_id: id },
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
