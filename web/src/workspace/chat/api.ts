/**
 * The wire side of the workspace chat panel (#317, epic #311): the conversation so far (`GET
 * .../notes/chat`, read with the voice fields #315 added), the events of the topic's workspace
 * stream (`GET .../workspace/stream`) decoded one by one, and the one place typed messages are
 * sent from (`sendTyped`; #329 moves it to `POST .../workspace/messages`). Bodies are read
 * leniently, like `src/chat/api.ts`: unknown fields ignored, missing optional ones defaulted, and
 * an event that is not understood is `null` (the stream's list of events is open).
 */

import { type ReadResult, topicPath } from "../../desk/api";
import { generateNotes } from "../../topic/PrepareTopic";
import { isOverCap } from "../../pending/doubts";
import { errorCode } from "../../protocol";
import { type ChatRef, chatPath, readRefs, readRevision, type RevisionResult, streamTurn, type StreamHandlers } from "../../chat/api";

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
const optionalText = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const count = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0);

export type TurnOrigin = "typed" | "voice";

/** The raw stretch of the transcript a spoken request came from. */
export interface SpokenSpan {
  requestId: string | null;
  text: string;
  /** Milliseconds from the start of the session. */
  startMs: number;
  endMs: number;
}

/** One turn of `GET .../notes/chat` as the panel uses it. */
export interface HistoryTurn {
  time: string;
  /** `revise`, `explain` ("¿Por qué pusiste esto?"), or a kind a later task adds. */
  kind: string;
  turnId: string | null;
  origin: TurnOrigin;
  /** A voice turn's short line of what was asked; `null` when typed. */
  requestSummary: string | null;
  transcript: SpokenSpan | null;
  message: string;
  reply: string;
  applied: boolean;
  /** The applied change's summary. */
  summary: string | null;
  changedSections: string[];
  commit: string | null;
  undone: boolean;
  warning: string | null;
  refs: ChatRef[];
}

export interface WorkspaceHistory {
  turns: HistoryTurn[];
  canUndo: boolean;
}

function readSpan(value: unknown, requestId: string | null = null): SpokenSpan | null {
  if (!isObject(value) || typeof value.text !== "string") return null;
  return {
    requestId: optionalText(value.request_id) ?? requestId,
    text: value.text,
    startMs: count(value.t_start_ms),
    endMs: count(value.t_end_ms),
  };
}

function readHistoryTurn(body: unknown): HistoryTurn | null {
  if (!isObject(body) || typeof body.message !== "string" || typeof body.reply !== "string") return null;
  const transcript = readSpan(body.transcript);
  return {
    time: text(body.time),
    kind: text(body.kind, "revise") || "revise",
    turnId: optionalText(body.turn_id),
    origin: body.origin === "voice" || transcript !== null ? "voice" : "typed",
    requestSummary: optionalText(body.request_summary),
    transcript,
    message: body.message,
    reply: body.reply,
    applied: body.applied === true,
    summary: optionalText(body.summary),
    changedSections: strings(body.changed_sections),
    commit: optionalText(body.commit),
    undone: body.undone === true,
    warning: optionalText(body.warning),
    refs: readRefs(body.refs),
  };
}

export function readWorkspaceHistory(body: unknown): WorkspaceHistory | null {
  if (!isObject(body) || !Array.isArray(body.turns)) return null;
  const turns = body.turns.map(readHistoryTurn);
  if (turns.some((turn) => turn === null)) return null;
  return { turns: turns as HistoryTurn[], canUndo: body.can_undo === true };
}

/** `GET .../notes/chat`: the conversation with the editor, oldest turn first. */
export async function fetchWorkspaceHistory(subjectId: string, topicId: string): Promise<ReadResult<WorkspaceHistory>> {
  let response: Response;
  try {
    response = await fetch(chatPath(subjectId, topicId));
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
  const history = response.ok ? readWorkspaceHistory(body) : null;
  return history === null ? { kind: "error", status: response.status } : { kind: "ok", value: history };
}

export function workspaceStreamPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/workspace/stream`;
}

/** What a finished turn did, from a `turn.result` or a typed turn's own `result`. */
export interface TurnOutcome {
  turnId: string | null;
  requestId: string | null;
  kind: string;
  /** The student's message (a voice turn: the raw transcript); `null` when the result has none. */
  message: string | null;
  transcript: SpokenSpan | null;
  requestSummary: string | null;
  reply: string;
  applied: boolean;
  summary: string | null;
  notesChanged: boolean;
  changedSections: string[];
  /** Unified diff of `apuntes.md`, `null` when there is none. */
  diff: string | null;
  commit: string | null;
  warning: string | null;
}

/** The reply a "prepárame el tema" turn shows, which has no reply of its own. */
function generationReply(body: Json): string {
  if (body.draft === true) return "He preparado un borrador de los apuntes, pero no ha pasado la validación.";
  return typeof body.version === "number"
    ? `He preparado los apuntes del tema (versión ${body.version}).`
    : "He preparado los apuntes del tema.";
}

export function readOutcome(body: unknown): TurnOutcome | null {
  if (!isObject(body)) return null;
  const kind = text(body.kind, "revise") || "revise";
  const request = isObject(body.request) ? body.request : null;
  const requestId = optionalText(body.request_id) ?? (request !== null ? optionalText(request.request_id) : null);
  if (kind === "prepare_notes") {
    return {
      turnId: optionalText(body.turn_id),
      requestId,
      kind,
      message: null,
      transcript: null,
      requestSummary: null,
      reply: generationReply(body),
      applied: body.draft !== true,
      summary: null,
      notesChanged: body.draft !== true,
      changedSections: [],
      diff: null,
      commit: optionalText(body.commit),
      warning: optionalText(body.warning),
    };
  }
  const revision: RevisionResult | null = readRevision(body);
  if (revision === null) return null;
  return {
    turnId: optionalText(body.turn_id),
    requestId,
    kind,
    message: revision.message !== "" ? revision.message : null,
    transcript: readSpan(request, requestId),
    requestSummary: request !== null ? optionalText(request.summary) : null,
    reply: revision.reply,
    applied: revision.applied,
    summary: revision.summary,
    notesChanged: revision.notes_changed,
    changedSections: revision.changed_sections,
    diff: revision.diff !== "" ? revision.diff : null,
    commit: revision.commit,
    warning: revision.warning,
  };
}

/** One event of the workspace stream the panel understands. */
export type WorkspaceEvent =
  | { type: "request.detected"; requestId: string; kind: string; summary: string; transcript: SpokenSpan | null }
  | { type: "turn.started"; turnId: string; requestId: string | null; origin: TurnOrigin; kind: string }
  | { type: "reply.delta"; turnId: string; text: string; attempt: number }
  | { type: "reply.restart"; turnId: string; attempt: number }
  | { type: "turn.result"; turnId: string; outcome: TurnOutcome }
  | { type: "turn.error"; turnId: string | null; requestId: string | null; status: number; detail: string; overCap: boolean }
  | { type: "notes.changed"; revision: string | null; origin: string; summary: string | null; turnId: string | null };

function parseData(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

const attemptOf = (value: unknown): number => (typeof value === "number" && value >= 1 ? value : 1);

/** Decodes one Server-Sent Event of the workspace stream; `null` for anything else. */
export function readWorkspaceEvent(event: string, data: string): WorkspaceEvent | null {
  const body = parseData(data);
  if (!isObject(body)) return null;
  const turnId = optionalText(body.turn_id);
  const requestId = optionalText(body.request_id);
  switch (event) {
    case "request.detected":
      return requestId === null
        ? null
        : {
            type: event,
            requestId,
            kind: text(body.kind, "edit"),
            summary: text(body.summary),
            transcript: readSpan(body.transcript, requestId),
          };
    case "turn.started":
      return turnId === null
        ? null
        : {
            type: event,
            turnId,
            requestId,
            origin: body.origin === "voice" ? "voice" : "typed",
            kind: text(body.kind, "revise") || "revise",
          };
    case "reply.delta":
      return turnId === null ? null : { type: event, turnId, text: text(body.text), attempt: attemptOf(body.attempt) };
    case "reply.restart":
      return turnId === null ? null : { type: event, turnId, attempt: attemptOf(body.attempt) };
    case "turn.result": {
      const outcome = readOutcome(body);
      return turnId === null || outcome === null ? null : { type: event, turnId, outcome };
    }
    case "turn.error": {
      const code = errorCode(body);
      return {
        type: event,
        turnId,
        requestId,
        status: typeof body.status === "number" ? body.status : 500,
        detail: optionalText(body.detail) ?? "El asistente no pudo completar la petición.",
        overCap: isOverCap(code),
      };
    }
    case "notes.changed":
      return {
        type: event,
        revision: optionalText(body.revision),
        origin: text(body.origin),
        summary: optionalText(body.summary),
        turnId,
      };
    default:
      return null;
  }
}

/** The failure of a typed message or a retried request, in Spanish. */
export interface SendFailure {
  detail: string;
  overCap: boolean;
  /** Refused before any turn started (e.g. another operation holds the notes). */
  beforeTurn: boolean;
}

export type SendOutcome = { kind: "ok"; outcome: TurnOutcome } | { kind: "failed"; failure: SendFailure } | { kind: "interrupted" };

const BEFORE_TURN = new Set([404, 409, 422, 503]);

const readTypedOutcome = (body: unknown): TurnOutcome | null => readOutcome(body);

function failureOf(result: { kind: "refused"; status: number; detail: string; overCap: boolean } | { kind: "error"; status: number } | { kind: "unreachable" }): SendFailure {
  switch (result.kind) {
    case "refused":
      // A reached cost cap, a Claude failure (502) or a crash (500) come from inside the turn; an
      // unknown topic, another operation holding the notes, an invalid body or no editor (404,
      // 409, 422, 503) are answered before any turn starts.
      return { detail: result.detail, overCap: result.overCap, beforeTurn: !result.overCap && BEFORE_TURN.has(result.status) };
    case "error":
      return { detail: `El servidor respondió con un error (${result.status}).`, overCap: false, beforeTurn: false };
    case "unreachable":
      return { detail: "No se pudo conectar con el servidor.", overCap: false, beforeTurn: true };
  }
}

/**
 * Sends one typed message (today `POST .../notes/chat`, streamed; #329 switches the target here
 * only). `handlers` get the reply as the typed turn's own stream writes it.
 */
export async function sendTyped(
  subjectId: string,
  topicId: string,
  message: string,
  { confirmOverCap = false, ...handlers }: StreamHandlers & { confirmOverCap?: boolean } = {},
): Promise<SendOutcome> {
  const result = await streamTurn(
    chatPath(subjectId, topicId),
    { message, confirm_over_cap: confirmOverCap },
    readTypedOutcome,
    handlers,
  );
  if (result.kind === "ok") return { kind: "ok", outcome: result.value };
  if (result.kind === "interrupted") return { kind: "interrupted" };
  return { kind: "failed", failure: failureOf(result) };
}

/** "Continuar igualmente" on a "prepárame el tema" stopped at the cost cap. */
export async function confirmPrepareNotes(subjectId: string, topicId: string): Promise<SendOutcome> {
  const result = await generateNotes(subjectId, topicId, true);
  if (result.kind !== "ok") return { kind: "failed", failure: failureOf(result) };
  const { draft, version, warning } = result.value;
  return {
    kind: "ok",
    outcome: {
      turnId: null,
      requestId: null,
      kind: "prepare_notes",
      message: null,
      transcript: null,
      requestSummary: null,
      reply: generationReply({ draft, version }),
      applied: !draft,
      summary: null,
      notesChanged: !draft,
      changedSections: [],
      diff: null,
      commit: null,
      warning,
    },
  };
}

export const DISCONNECTED_TURN = "Se cortó la conexión con el asistente antes de terminar; el documento muestra lo que quedó guardado.";
