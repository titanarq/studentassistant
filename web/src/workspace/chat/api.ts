/**
 * The wire side of the workspace chat panel (#317, epic #311): the conversation so far (`GET
 * .../notes/chat`, read with the voice fields #315 added), the events of the topic's workspace
 * stream (`GET .../workspace/stream`) decoded one by one, and typed messages, which go to the
 * request classifier (`postMessage`, `POST .../workspace/messages`, #327/#329), as does "Continuar
 * igualmente" past the cost cap (`confirmOverCap`, #351). Bodies are read
 * leniently, like `src/chat/api.ts`: unknown fields ignored, missing optional ones defaulted, and
 * an event that is not understood is `null` (the stream's list of events is open).
 */

import { type ReadResult, topicPath } from "../../desk/api";
import { isOverCap } from "../../pending/doubts";
import { errorCode } from "../../protocol";
import { type ChatRef, chatPath, readRefs, readRevision, type RevisionResult } from "../../chat/api";

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
const optionalText = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const count = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0);

export type TurnOrigin = "typed" | "voice";

/** One side of a contradiction: a source and what it says. */
export interface DoubtOption {
  sourceId: string;
  says: string;
}

/** A doubt asked in the chat (#325): `doubt.asked`, or a history turn of kind `doubt`. */
export interface DoubtView {
  pendingId: string;
  question: string;
  suggestions: string[];
  options: DoubtOption[];
  /** The sources the doubt is about (topic-relative ids). */
  refs: string[];
  /** `open` until answered; then `resolved`, `auto_resolved` or `dismissed`. */
  status: string;
  resolution: string | null;
  /** What the student answered, when the history knows it. */
  answer: string | null;
}

function readOptions(value: unknown): DoubtOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((option) =>
    isObject(option) && typeof option.source_id === "string" ? [{ sourceId: option.source_id, says: text(option.says) }] : [],
  );
}

function readDoubt(body: Json, refs: unknown): DoubtView | null {
  const pendingId = optionalText(body.pending_id);
  const question = optionalText(body.question) ?? optionalText(body.reply);
  if (pendingId === null || question === null) return null;
  return {
    pendingId,
    question,
    suggestions: strings(body.suggestions),
    options: readOptions(body.options),
    refs: strings(refs),
    status: text(body.status, "open") || "open",
    resolution: optionalText(body.resolution),
    answer: optionalText(body.answer),
  };
}

/**
 * One target of a set-aside asked in the chat and why (#351): its triage reasons (`blank`,
 * `duplicate`, `blurry`, `partial`, `same_content`, as `sources.triage` names them), the page it
 * repeats, and whether it was set aside before the request.
 */
export interface TriageTarget {
  sourceId: string;
  reasons: string[];
  duplicateOf: string | null;
  already: boolean;
}

function readTargets(value: unknown): TriageTarget[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((target) =>
    isObject(target) && typeof target.source_id === "string"
      ? [{ sourceId: target.source_id, reasons: strings(target.reasons), duplicateOf: optionalText(target.duplicate_of), already: target.already === true }]
      : [],
  );
}

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
  /** `incorporate`: the unified diff of `apuntes.md` (other turns have none in the history). */
  diff: string | null;
  /** `incorporate`: the sources incorporated; `triage`: the ones set aside or restored. */
  sourceIds: string[];
  /** `triage`: what was done. */
  decision: TriageDecision | null;
  /** `triage` (`set_aside`): each target and its reasons; empty for a restore or an old turn. */
  targets: TriageTarget[];
  /** `doubt`: the doubt asked. */
  doubt: DoubtView | null;
  /** `doubts_resolved`: the doubts the editor settled from the sources. */
  pendingIds: string[];
}

export type TriageDecision = "set_aside" | "restore";

const decisionOf = (value: unknown): TriageDecision | null => (value === "set_aside" || value === "restore" ? value : null);

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
    diff: optionalText(body.diff),
    sourceIds: strings(body.source_ids),
    decision: body.kind === "triage" ? decisionOf(body.summary) : null,
    targets: body.kind === "triage" ? readTargets(body.targets) : [],
    doubt: body.kind === "doubt" ? readDoubt(body, body.doubt_refs) : null,
    pendingIds: strings(body.pending_ids),
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
  /** `incorporate` / `triage`: the sources incorporated, set aside or restored. */
  sourceIds: string[];
  /** `incorporate`: how many doubts it raised. */
  doubts: number;
  decision: TriageDecision | null;
  /** `triage` (`set_aside`): each target and its reasons. */
  targets: TriageTarget[];
  /** `doubt_answer`: the doubt answered. */
  pendingId: string | null;
  /** `study` ("quiero estudiar", #335): where the assistant's one button goes. */
  action: GoStudyAction | null;
}

/** The action of a `study` turn: the "Ir a Estudiar" button to the study screen. */
export interface GoStudyAction {
  kind: "go_study";
  path: string;
}

const OUTCOME_EXTRAS = {
  sourceIds: [],
  doubts: 0,
  decision: null,
  targets: [],
  pendingId: null,
  action: null,
} satisfies Partial<TurnOutcome>;

/** A `go_study` action with an in-app path, else null. */
function readAction(value: unknown): GoStudyAction | null {
  if (!isObject(value) || value.kind !== "go_study") return null;
  const path = optionalText(value.path);
  return path !== null && path.startsWith("/") && !path.startsWith("//") ? { kind: "go_study", path } : null;
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
      ...OUTCOME_EXTRAS,
    };
  }
  if (kind === "doubt_answer") {
    // A `ResolutionResult`: the resolution is what the chat shows as the reply.
    const pendingId = optionalText(body.pending_id);
    if (pendingId === null) return null;
    const changed = body.notes_changed === true;
    return {
      turnId: optionalText(body.turn_id),
      requestId,
      kind,
      message: null,
      transcript: null,
      requestSummary: null,
      reply: optionalText(body.resolution) ?? (body.status === "dismissed" ? "Descartada." : "Anotado."),
      applied: changed,
      summary: null,
      notesChanged: changed,
      changedSections: [],
      diff: null,
      commit: optionalText(body.commit),
      warning: optionalText(body.warning),
      ...OUTCOME_EXTRAS,
      pendingId,
    };
  }
  if (kind === "study") {
    // A `StudyTurn`: the topic switched to Estudiar; one line and the "Ir a Estudiar" action.
    return {
      turnId: optionalText(body.turn_id),
      requestId,
      kind,
      message: optionalText(body.message),
      transcript: readSpan(request, requestId),
      requestSummary: request !== null ? optionalText(request.summary) : null,
      reply: text(body.reply),
      applied: false,
      summary: null,
      notesChanged: false,
      changedSections: [],
      diff: null,
      commit: null,
      warning: null,
      ...OUTCOME_EXTRAS,
      action: readAction(body.action),
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
    sourceIds: strings(body.source_ids),
    doubts: Array.isArray(body.doubts) ? body.doubts.length : 0,
    // A `TriageTurn` of a `set_aside` / `restore` turn.
    decision: decisionOf(body.decision) ?? decisionOf(kind),
    targets: readTargets(body.targets),
    pendingId: null,
    action: null,
  };
}

/** One event of the workspace stream the panel understands. */
export type WorkspaceEvent =
  | {
      type: "request.detected";
      requestId: string;
      kind: string;
      summary: string;
      origin: TurnOrigin;
      transcript: SpokenSpan | null;
    }
  | { type: "turn.started"; turnId: string; requestId: string | null; origin: TurnOrigin; kind: string }
  | { type: "reply.delta"; turnId: string; text: string; attempt: number }
  | { type: "reply.restart"; turnId: string; attempt: number }
  | { type: "turn.result"; turnId: string; outcome: TurnOutcome }
  | { type: "turn.error"; turnId: string | null; requestId: string | null; status: number; detail: string; overCap: boolean }
  | { type: "notes.changed"; revision: string | null; origin: string; summary: string | null; turnId: string | null }
  | { type: "doubt.asked"; doubt: DoubtView }
  | { type: "doubt.resolved"; pendingId: string; status: string; resolution: string | null; notesChanged: boolean }
  | { type: "doubts.auto_resolved"; pendingIds: string[]; summary: string }
  | { type: "incorporation.progress"; done: number; total: number; sourceIds: string[] };

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
            origin: body.origin === "typed" ? "typed" : "voice",
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
    case "doubt.asked": {
      const doubt = readDoubt({ ...body, status: "open" }, body.refs);
      return doubt === null ? null : { type: event, doubt };
    }
    case "doubt.resolved": {
      const pendingId = optionalText(body.pending_id);
      return pendingId === null
        ? null
        : {
            type: event,
            pendingId,
            status: text(body.status, "resolved") || "resolved",
            resolution: optionalText(body.resolution),
            notesChanged: body.notes_changed === true,
          };
    }
    case "doubts.auto_resolved": {
      const pendingIds = strings(body.pending_ids);
      return pendingIds.length === 0 ? null : { type: event, pendingIds, summary: text(body.summary) };
    }
    case "incorporation.progress":
      return { type: event, done: count(body.done), total: count(body.total), sourceIds: strings(body.source_ids) };
    default:
      return null;
  }
}

/** One request the classifier made of a typed message (`AssistantRequest`). */
export interface PostedRequest {
  requestId: string;
  kind: string;
  summary: string;
  text: string;
  targets: string[];
}

export type PostOutcome =
  | { kind: "ok"; messageId: string | null; requests: PostedRequest[]; classified: boolean }
  | { kind: "failed"; detail: string };

function readPosted(value: unknown): PostedRequest | null {
  if (!isObject(value)) return null;
  const requestId = optionalText(value.request_id);
  return requestId === null
    ? null
    : {
        requestId,
        kind: text(value.kind, "edit"),
        summary: text(value.summary),
        text: text(value.text),
        targets: strings(value.targets),
      };
}

export function workspaceMessagesPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/workspace/messages`;
}

/**
 * `POST .../workspace/messages` `{text}` (#327): the message goes to the request classifier like a
 * spoken one and is answered 202 with the requests it became, queued in the backend; their turns
 * then come through the workspace stream. A refusal is its Spanish `detail`. With a Recursos
 * selection (#432) the body is `{text, selected_source_ids}` (topic-relative ids, in the order they
 * were selected), which the backend resolves «esto», «estas páginas» against (#433).
 */
export function postMessage(
  subjectId: string,
  topicId: string,
  message: string,
  selectedSourceIds: readonly string[] = [],
): Promise<PostOutcome> {
  const payload: Json = { text: message };
  // The Recursos selection (#432, #433): only when there is one, else the body is `{text}` as before.
  if (selectedSourceIds.length > 0) payload.selected_source_ids = [...selectedSourceIds];
  return postToMessages(subjectId, topicId, payload);
}

/**
 * "Continuar igualmente" (#351): `POST .../workspace/messages` `{confirm_over_cap: true, turn_id}`
 * queues again, confirmed past the cost cap, the request whose turn `turnId` stopped at the cap --
 * as it was classified, whatever its kind -- and answers 202 with it; its new turn comes through
 * the workspace stream with the same `request_id`. A refusal (e.g. 404: it no longer waits for a
 * confirmation) is its Spanish `detail`.
 */
export function confirmOverCap(subjectId: string, topicId: string, turnId: string): Promise<PostOutcome> {
  return postToMessages(subjectId, topicId, { confirm_over_cap: true, turn_id: turnId });
}

async function postToMessages(subjectId: string, topicId: string, payload: Json): Promise<PostOutcome> {
  let response: Response;
  try {
    response = await fetch(workspaceMessagesPath(subjectId, topicId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    return { kind: "failed", detail: "No se pudo conectar con el servidor." };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    const detail = isObject(body) ? optionalText(body.detail) : null;
    return { kind: "failed", detail: detail ?? `El servidor respondió con un error (${response.status}).` };
  }
  if (!isObject(body) || !Array.isArray(body.requests)) {
    return { kind: "failed", detail: "El servidor respondió algo inesperado." };
  }
  return {
    kind: "ok",
    messageId: optionalText(body.message_id),
    requests: body.requests.map(readPosted).filter((r): r is PostedRequest => r !== null),
    classified: body.classified !== false,
  };
}
