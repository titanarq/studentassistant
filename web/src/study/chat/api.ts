/**
 * The study chat's reading of `POST .../tutor` with `style: "written"` (#336, #366, #367). Besides
 * the tutor's answer (`reply.delta`, then `result`), a request such as «hazme un quiz» makes the
 * backend generate that material on the same stream:
 * - `generation.started` `{kind, option, text}`: what is being prepared, in Spanish;
 * - `result` with `kind: "generation"` `{option, material_kind, reply, items, warnings, study}`,
 *   `study` being the fresh `StudyState` of `GET .../study`;
 * - or `error`, exactly as the tutor's.
 * A `result` without `kind` (or `kind: "answer"`) is the tutor's answer, read as before.
 */

import type { StreamHandlers, StreamOutcome } from "../../chat/api";
import { streamTurn } from "../../chat/api";
import { readItems, readTutorAnswer, type TutorAnswer, tutorPath } from "../../tutor/api";
import { readStudyState, type StudyState } from "../api";

/** `generation.started`: the material being prepared. */
export interface GenerationStarted {
  /** The generator kind (`quiz`, `examen`, ...). */
  kind: string;
  /** The study option key (`quiz`, `ejercicios`, ...) or `diapositivas`. */
  option: string;
  /** A Spanish line, e.g. "Preparando un quiz de 10 preguntas con tus apuntes v4…". */
  text: string;
}

/** A `result` with `kind: "generation"`. */
export interface GenerationResult {
  option: string;
  materialKind: string;
  reply: string;
  items: number | null;
  warnings: string[];
  /** The topic's study state after the generation (null: the body carried none the page reads). */
  study: StudyState | null;
}

export type StudyChatReply = { kind: "answer"; answer: TutorAnswer } | { kind: "generation"; generation: GenerationResult };

export type StudyChatOutcome = StreamOutcome<StudyChatReply>;

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string => (typeof value === "string" ? value : "");

export function readGenerationStarted(payload: Json): GenerationStarted {
  return { kind: text(payload.kind), option: text(payload.option), text: text(payload.text) };
}

export function readGenerationResult(body: unknown): GenerationResult | null {
  if (!isObject(body) || typeof body.reply !== "string") return null;
  return {
    option: text(body.option),
    materialKind: text(body.material_kind),
    reply: body.reply,
    items: readItems(body.items),
    warnings: Array.isArray(body.warnings)
      ? body.warnings.filter((w): w is string => typeof w === "string" && w !== "")
      : [],
    study: readStudyState(body.study),
  };
}

export function readStudyChatReply(body: unknown): StudyChatReply | null {
  if (isObject(body) && body.kind === "generation") {
    const generation = readGenerationResult(body);
    return generation === null ? null : { kind: "generation", generation };
  }
  const answer = readTutorAnswer(body);
  return answer === null ? null : { kind: "answer", answer };
}

/** `POST .../tutor` in the written style: a question, or a request to generate a material. */
export function askStudyChat(
  subjectId: string,
  topicId: string,
  question: string,
  {
    confirmOverCap = false,
    onGenerationStarted,
    ...handlers
  }: StreamHandlers & { confirmOverCap?: boolean; onGenerationStarted?: (started: GenerationStarted) => void } = {},
): Promise<StudyChatOutcome> {
  return streamTurn(
    tutorPath(subjectId, topicId),
    { question, confirm_over_cap: confirmOverCap, style: "written" },
    readStudyChatReply,
    {
      ...handlers,
      onEvent: (event, payload) => {
        if (event === "generation.started") onGenerationStarted?.(readGenerationStarted(payload));
        handlers.onEvent?.(event, payload);
      },
    },
  );
}
