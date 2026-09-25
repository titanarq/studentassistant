// Maps each `protocol/<name>.schema.json` name to the decoder that parses it (mirrors
// `studentassistant.protocol.MODELS`).

import * as client from "./client";
import type { Decoder } from "./decode";
import * as rest from "./rest";
import * as server from "./server";

export interface MessageTypes {
  "client.hello": client.ClientHello;
  "client.transcript.client.partial": client.TranscriptClientPartial;
  "client.transcript.client.final": client.TranscriptClientFinal;
  "client.button": client.Button;
  "client.marker": client.Marker;
  "client.ack": client.ClientAck;
  "server.hello.ack": server.HelloAck;
  "server.transcript.partial": server.TranscriptPartial;
  "server.transcript.final": server.TranscriptFinal;
  "server.command": server.Command;
  "server.notice": server.Notice;
  "server.stt.status": server.SttStatus;
  "server.ack": server.ServerAck;
  "rest.pair.request": rest.PairRequest;
  "rest.pair.response": rest.PairResponse;
  "rest.health.response": rest.HealthResponse;
  "rest.subjects.list.response": rest.SubjectsListResponse;
  "rest.subjects.create.request": rest.SubjectCreateRequest;
  "rest.subjects.create.response": rest.Subject;
  "rest.topics.list.response": rest.TopicsListResponse;
  "rest.topics.create.request": rest.TopicCreateRequest;
  "rest.topics.create.response": rest.Topic;
  "rest.sessions.start.request": rest.SessionStartRequest;
  "rest.sessions.start.response": rest.Session;
  "rest.sessions.resume.response": rest.Session;
  "rest.sessions.end.request": rest.SessionEndRequest;
  "rest.sessions.end.response": rest.SessionEndResponse;
  "rest.sessions.captures.request": rest.CaptureUploadRequest;
  "rest.sessions.captures.response": rest.CaptureUploadResponse;
  "rest.search.response": rest.SearchResponse;
  "rest.topics.web_pages.create.request": rest.WebPageAddRequest;
  "rest.topics.web_pages.create.response": rest.WebPageAddResponse;
}

export type MessageName = keyof MessageTypes;

export const DECODERS: { [N in MessageName]: Decoder<MessageTypes[N]> } = {
  "client.hello": client.decodeClientHello,
  "client.transcript.client.partial": client.decodeTranscriptClientPartial,
  "client.transcript.client.final": client.decodeTranscriptClientFinal,
  "client.button": client.decodeButton,
  "client.marker": client.decodeMarker,
  "client.ack": client.decodeClientAck,
  "server.hello.ack": server.decodeHelloAck,
  "server.transcript.partial": server.decodeTranscriptPartial,
  "server.transcript.final": server.decodeTranscriptFinal,
  "server.command": server.decodeCommand,
  "server.notice": server.decodeNotice,
  "server.stt.status": server.decodeSttStatus,
  "server.ack": server.decodeServerAck,
  "rest.pair.request": rest.decodePairRequest,
  "rest.pair.response": rest.decodePairResponse,
  "rest.health.response": rest.decodeHealthResponse,
  "rest.subjects.list.response": rest.decodeSubjectsListResponse,
  "rest.subjects.create.request": rest.decodeSubjectCreateRequest,
  "rest.subjects.create.response": rest.decodeSubject,
  "rest.topics.list.response": rest.decodeTopicsListResponse,
  "rest.topics.create.request": rest.decodeTopicCreateRequest,
  "rest.topics.create.response": rest.decodeTopic,
  "rest.sessions.start.request": rest.decodeSessionStartRequest,
  "rest.sessions.start.response": rest.decodeSession,
  "rest.sessions.resume.response": rest.decodeSession,
  "rest.sessions.end.request": rest.decodeSessionEndRequest,
  "rest.sessions.end.response": rest.decodeSessionEndResponse,
  "rest.sessions.captures.request": rest.decodeCaptureUploadRequest,
  "rest.sessions.captures.response": rest.decodeCaptureUploadResponse,
  "rest.search.response": rest.decodeSearchResponse,
  "rest.topics.web_pages.create.request": rest.decodeWebPageAddRequest,
  "rest.topics.web_pages.create.response": rest.decodeWebPageAddResponse,
};

export function isMessageName(name: string): name is MessageName {
  return Object.hasOwn(DECODERS, name);
}

/** Decodes `data` as the message `name` (a schema name such as `client.hello`). */
export function parseMessage<N extends MessageName>(name: N, data: unknown): MessageTypes[N] {
  return DECODERS[name](data, "");
}
