// Messages the backend sends over `/ws/sessions/{id}` (`server.*` schemas).
// `server_time_ms` is the backend clock in Unix epoch ms; `session_*_ms` count milliseconds since
// the session's `started_at_ms` (ADR-0008 session time).

import { decodeAudioFormat, type AudioFormat } from "./client";
import {
  array,
  captureId,
  confidence,
  type Decoder,
  discriminated,
  epochMs,
  id,
  int,
  language,
  literal,
  object,
  refine,
  str,
} from "./decode";
import { protocolVersion } from "./rest";

/** Since 1.4: the most terms `vocabulary_hints` carries, and the longest term. */
export const VOCABULARY_HINTS_MAX_ITEMS = 50;
export const VOCABULARY_HINT_MAX_CHARS = 100;

/** Reply to the client `hello`: negotiated version, chosen STT mode and clock offset. */
export interface HelloAck {
  type: "hello.ack";
  /** The version both sides speak (shared MAJOR, lower MINOR). */
  protocol_version: string;
  /** `server`: the client must stream binary audio frames instead of its own transcripts. */
  stt_mode: "client" | "server";
  /** Present exactly when `stt_mode` is `server`. */
  audio_format?: AudioFormat;
  /** Backend clock minus the client clock read in `hello`; may be negative. */
  clock_offset_ms: number;
  server_time_ms: number;
  /** Since 1.4: domain terms (subject, topic, concepts) a recognizer may be biased towards. */
  vocabulary_hints?: string[];
}

interface NormalisedSegment {
  segment_id: string;
  session_start_ms: number;
  session_end_ms: number;
  text: string;
  language: string;
  confidence?: number;
}

/** Interim text of an utterance still being spoken; a later one replaces it. */
export interface TranscriptPartial extends NormalisedSegment {
  type: "transcript.partial";
}

/** The settled text of an utterance; it replaces every partial with the same `segment_id`. */
export interface TranscriptFinal extends NormalisedSegment {
  type: "transcript.final";
}

/** A voice command asks the client to act (ADR-0006); answered by the client `ack`. */
export interface Command {
  type: "command";
  command_id: string;
  /** `capture_now`: take a burst of stills and upload them with `trigger: command`. */
  command: "capture_now";
  server_time_ms: number;
}

/** Status the client shows the student, such as how many doubts await review. */
export interface Notice {
  type: "notice";
  pending_count: number;
  server_time_ms: number;
  /** Since 1.4: the session's new vocabulary hints, replacing the previous list. */
  vocabulary_hints?: string[];
}

/** Since 1.5: the longest `stt.status` detail. */
export const STT_STATUS_DETAIL_MAX_CHARS = 300;

/**
 * Since 1.5: the backend's own STT provider (server STT mode) changed between working and
 * degraded. `ok` clears any warning; `reconnecting` and `unavailable` mean the audio is not being
 * transcribed, and `detail` says so in Spanish.
 */
export interface SttStatus {
  type: "stt.status";
  state: "ok" | "reconnecting" | "unavailable";
  detail?: string;
  server_time_ms: number;
}

/** The backend stored audio up to a frame `seq` and/or the listed captures. */
export interface ServerAck {
  type: "ack";
  audio_seq?: number;
  capture_ids?: string[];
  server_time_ms: number;
}

export type ServerEvent =
  | HelloAck
  | TranscriptPartial
  | TranscriptFinal
  | Command
  | Notice
  | SttStatus
  | ServerAck;

// Decoders

const sessionMs = int({ min: 0 });
const vocabularyHints = array(str({ minLength: 1, maxLength: VOCABULARY_HINT_MAX_CHARS }), {
  minItems: 1,
  maxItems: VOCABULARY_HINTS_MAX_ITEMS,
});

export const decodeHelloAck: Decoder<HelloAck> = refine(
  object(
    {
      type: literal("hello.ack"),
      protocol_version: protocolVersion,
      stt_mode: literal("client", "server"),
      clock_offset_ms: int(),
      server_time_ms: epochMs,
    },
    { audio_format: decodeAudioFormat, vocabulary_hints: vocabularyHints },
  ),
  (h) =>
    (h.stt_mode === "server") !== (h.audio_format !== undefined)
      ? "audio_format is required in server stt_mode and forbidden otherwise"
      : null,
);

function segment<const T extends string>(type: T) {
  return refine(
    object(
      {
        type: literal(type),
        segment_id: id,
        session_start_ms: sessionMs,
        session_end_ms: sessionMs,
        text: str(),
        language,
      },
      { confidence },
    ),
    (s) => (s.session_end_ms < s.session_start_ms ? "session_end_ms is before session_start_ms" : null),
  );
}

export const decodeTranscriptPartial: Decoder<TranscriptPartial> = segment("transcript.partial");
export const decodeTranscriptFinal: Decoder<TranscriptFinal> = segment("transcript.final");

export const decodeCommand: Decoder<Command> = object({
  type: literal("command"),
  command_id: id,
  command: literal("capture_now"),
  server_time_ms: epochMs,
});

export const decodeNotice: Decoder<Notice> = object(
  {
    type: literal("notice"),
    pending_count: int({ min: 0 }),
    server_time_ms: epochMs,
  },
  { vocabulary_hints: vocabularyHints },
);

export const decodeSttStatus: Decoder<SttStatus> = object(
  {
    type: literal("stt.status"),
    state: literal("ok", "reconnecting", "unavailable"),
    server_time_ms: epochMs,
  },
  { detail: str({ minLength: 1, maxLength: STT_STATUS_DETAIL_MAX_CHARS }) },
);

export const decodeServerAck: Decoder<ServerAck> = refine(
  object(
    { type: literal("ack"), server_time_ms: epochMs },
    { audio_seq: int({ min: 0, max: 2 ** 32 - 1 }), capture_ids: array(captureId, { minItems: 1 }) },
  ),
  (a) =>
    a.audio_seq === undefined && a.capture_ids === undefined
      ? "an ack needs audio_seq, capture_ids or both"
      : null,
);

/** Decodes one parsed JSON server message; throws `ProtocolDecodeError` on an unknown `type`. */
export const decodeServerEvent: Decoder<ServerEvent> = discriminated<ServerEvent>({
  "hello.ack": decodeHelloAck,
  "transcript.partial": decodeTranscriptPartial,
  "transcript.final": decodeTranscriptFinal,
  command: decodeCommand,
  notice: decodeNotice,
  "stt.status": decodeSttStatus,
  ack: decodeServerAck,
});

export function parseServerEvent(data: unknown): ServerEvent {
  return decodeServerEvent(data, "");
}
