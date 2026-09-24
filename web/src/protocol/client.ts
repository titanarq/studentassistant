// Messages the capture client sends over `/ws/sessions/{id}` (`client.*` schemas).

import {
  confidence,
  type Decoder,
  discriminated,
  epochMs,
  id,
  int,
  language,
  literal,
  object,
  providerId,
  refine,
  str,
} from "./decode";
import { protocolVersion } from "./rest";

/** The only audio the backend accepts in server STT mode (ADR-0001, ADR-0008). */
export interface AudioFormat {
  encoding: "pcm16";
  sample_rate_hz: 16000;
  channels: 1;
}

/** What the client can do; the backend picks the STT mode in `hello.ack`. */
export interface ClientCapabilities {
  /** The client's preferred STT mode; the backend may still ask for the other one. */
  stt: "client" | "server";
  /** Id of the client's own recognizer, e.g. `web-speech`. */
  stt_provider: string;
  /** Present only when the client can stream audio for server-side STT. */
  audio_format?: AudioFormat;
}

/** First message on the WebSocket: version, capabilities and the clock-sync reading. */
export interface ClientHello {
  type: "hello";
  protocol_version: string;
  capabilities: ClientCapabilities;
  /** Client clock in Unix epoch ms when the message was sent. */
  client_time_ms: number;
}

interface TranscriptSegment {
  /** Client-assigned; the partials and the final of one utterance share it. */
  segment_id: string;
  client_start_ms: number;
  client_end_ms: number;
  text: string;
  provider: string;
  language: string;
  /** The recognizer's own score in [0, 1], when it reports one. */
  confidence?: number;
}

/** Interim text of an utterance still being spoken; a later one replaces it. */
export interface TranscriptClientPartial extends TranscriptSegment {
  type: "transcript.client.partial";
}

/** The settled text of an utterance; it replaces every partial with the same `segment_id`. */
export interface TranscriptClientFinal extends TranscriptSegment {
  type: "transcript.client.final";
}

/** Every ADR-0006 command except capture, which the client performs and uploads itself. */
export type ButtonName =
  | "next_page"
  | "important"
  | "switch_source"
  | "pause"
  | "resume"
  | "end_session"
  | "web_search";
export type SourceKind = "book" | "notes" | "pdf";

/** The student pressed the button equivalent of a voice command (ADR-0006). */
export interface Button {
  type: "button";
  button: ButtonName;
  /** Present exactly when `button` is `switch_source`. */
  source?: SourceKind;
  client_time_ms: number;
}

/** A point on the session timeline the student flagged, with an optional short label. */
export interface Marker {
  type: "marker";
  client_time_ms: number;
  label?: string;
}

/** The client received a server `command` and acted on it (ADR-0006). */
export interface ClientAck {
  type: "ack";
  command_id: string;
  client_time_ms: number;
}

export type ClientEvent =
  | ClientHello
  | TranscriptClientPartial
  | TranscriptClientFinal
  | Button
  | Marker
  | ClientAck;

// Decoders

export const decodeAudioFormat: Decoder<AudioFormat> = object({
  encoding: literal("pcm16"),
  sample_rate_hz: literal(16000),
  channels: literal(1),
});

export const decodeClientHello: Decoder<ClientHello> = object({
  type: literal("hello"),
  protocol_version: protocolVersion,
  capabilities: object(
    { stt: literal("client", "server"), stt_provider: providerId },
    { audio_format: decodeAudioFormat },
  ),
  client_time_ms: int({ min: 0 }),
});

function segment<const T extends string>(type: T) {
  return refine(
    object(
      {
        type: literal(type),
        segment_id: id,
        client_start_ms: epochMs,
        client_end_ms: epochMs,
        text: str(),
        provider: providerId,
        language,
      },
      { confidence },
    ),
    (s) => (s.client_end_ms < s.client_start_ms ? "client_end_ms is before client_start_ms" : null),
  );
}

export const decodeTranscriptClientPartial: Decoder<TranscriptClientPartial> =
  segment("transcript.client.partial");
export const decodeTranscriptClientFinal: Decoder<TranscriptClientFinal> =
  segment("transcript.client.final");

export const decodeButton: Decoder<Button> = refine(
  object(
    {
      type: literal("button"),
      button: literal<ButtonName>(
        "next_page",
        "important",
        "switch_source",
        "pause",
        "resume",
        "end_session",
        "web_search",
      ),
      client_time_ms: epochMs,
    },
    { source: literal<SourceKind>("book", "notes", "pdf") },
  ),
  (b) =>
    (b.button === "switch_source") !== (b.source !== undefined)
      ? "source is required with switch_source and forbidden otherwise"
      : null,
);

export const decodeMarker: Decoder<Marker> = object(
  { type: literal("marker"), client_time_ms: epochMs },
  { label: str({ minLength: 1, maxLength: 200 }) },
);

export const decodeClientAck: Decoder<ClientAck> = object({
  type: literal("ack"),
  command_id: id,
  client_time_ms: epochMs,
});

/** Decodes one parsed JSON client message; throws `ProtocolDecodeError` on an unknown `type`. */
export const decodeClientEvent: Decoder<ClientEvent> = discriminated<ClientEvent>({
  hello: decodeClientHello,
  "transcript.client.partial": decodeTranscriptClientPartial,
  "transcript.client.final": decodeTranscriptClientFinal,
  button: decodeButton,
  marker: decodeMarker,
  ack: decodeClientAck,
});

export function parseClientEvent(data: unknown): ClientEvent {
  return decodeClientEvent(data, "");
}
