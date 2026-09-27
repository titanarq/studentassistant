/**
 * Client of the voice tutor API (`server/tutor_routes.py`, #82): one question as a Server-Sent
 * Events stream (`POST .../tutor`, the same stream as the editor chat: `reply.delta`, then
 * `result` or `error`) and the topic's questions so far (`GET .../tutor`). Bodies are read
 * leniently, like the editor chat's.
 */

import { type ChatRef, readRefs, type StreamHandlers, type StreamOutcome, streamTurn } from "../chat/api";
import { type ReadResult, topicPath } from "../desk/api";

/**
 * How the tutor answers: `spoken` for the voice tutor (the default), `written` for the study
 * screen's question chat (#334, #336): short Markdown citing sections as `[§anchor]` too.
 */
export type TutorStyle = "spoken" | "written";

/** A section of the current notes a written answer cites (`[§anchor]`), with its heading's title. */
export interface SectionRef {
  anchor: string;
  title: string;
}

/** What the page uses of the backend's `TutorAnswer`, the `result` event of a question. */
export interface TutorAnswer {
  style: TutorStyle;
  question: string;
  /** The answer, with the notes' `[^label]` marks (see `spokenText`) and, written, `[§anchor]` ones. */
  reply: string;
  /** The notes' footnotes the answer cites, in order. */
  refs: ChatRef[];
  /** The sections of the notes a written answer cites, in order; empty when spoken. */
  sections: SectionRef[];
  warning: string | null;
}

/** One earlier question and its answer. */
export interface TutorTurn extends TutorAnswer {
  time: string;
}

export type TutorOutcome = StreamOutcome<TutorAnswer>;

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const optionalText = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

export function readSections(value: unknown): SectionRef[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((section) =>
    isObject(section) && typeof section.anchor === "string" && section.anchor !== ""
      ? [{ anchor: section.anchor, title: optionalText(section.title) ?? section.anchor }]
      : [],
  );
}

export function readTutorAnswer(body: unknown): TutorAnswer | null {
  if (!isObject(body) || typeof body.reply !== "string" || typeof body.question !== "string") return null;
  return {
    // Turns recorded before #334 carry no style: they were spoken.
    style: body.style === "written" ? "written" : "spoken",
    question: body.question,
    reply: body.reply,
    refs: readRefs(body.refs),
    sections: readSections(body.sections),
    warning: optionalText(body.warning),
  };
}

export function readTutorHistory(body: unknown): TutorTurn[] | null {
  if (!isObject(body) || !Array.isArray(body.turns)) return null;
  const turns: TutorTurn[] = [];
  for (const raw of body.turns) {
    const answer = readTutorAnswer(raw);
    if (answer === null) return null;
    turns.push({ ...answer, time: isObject(raw) && typeof raw.time === "string" ? raw.time : "" });
  }
  return turns;
}

export function tutorPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/tutor`;
}

/** `GET .../tutor`: the topic's questions to the tutor, oldest first. */
export async function fetchTutorHistory(subjectId: string, topicId: string): Promise<ReadResult<TutorTurn[]>> {
  let response: Response;
  try {
    response = await fetch(tutorPath(subjectId, topicId));
  } catch {
    return { kind: "unreachable" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (response.status === 404) {
    const detail = isObject(body) ? optionalText(body.detail) : null;
    return { kind: "not-found", detail: detail ?? "No encontrado." };
  }
  const turns = response.ok ? readTutorHistory(body) : null;
  return turns === null ? { kind: "error", status: response.status } : { kind: "ok", value: turns };
}

/**
 * `POST .../tutor`: asks one question and reads the answer's stream. Without `style` the body
 * carries none and the backend answers `spoken`.
 */
export function askTutor(
  subjectId: string,
  topicId: string,
  question: string,
  {
    confirmOverCap = false,
    style,
    ...handlers
  }: StreamHandlers & { confirmOverCap?: boolean; style?: TutorStyle } = {},
): Promise<TutorOutcome> {
  const body: Record<string, unknown> = { question, confirm_over_cap: confirmOverCap };
  if (style !== undefined) body.style = style;
  return streamTurn(tutorPath(subjectId, topicId), body, readTutorAnswer, handlers);
}

export function describeTutorFailure(result: Exclude<TutorOutcome, { kind: "ok" }>): string {
  switch (result.kind) {
    case "refused":
      return result.detail;
    case "error":
      return `El servidor respondió con un error (${result.status}).`;
    case "unreachable":
      return "No se pudo conectar con el servidor.";
    case "interrupted":
      return "Se cortó la conexión con el tutor antes de terminar la respuesta.";
  }
}
