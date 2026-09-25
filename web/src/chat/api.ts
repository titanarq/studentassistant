/**
 * Client of the editor chat API (`server/revise_routes.py`, #63): one chat turn as a
 * Server-Sent Events stream (`POST .../notes/chat`), the conversation so far (`GET .../notes/chat`)
 * and the undo of the latest applied turn (`POST .../notes/chat/undo`). Bodies are read leniently
 * (unknown fields ignored, missing optional ones defaulted), like the doubts results.
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type ActionResult, isOverCap, postAction } from "../pending/doubts";
import { errorCode } from "../protocol";
import { readSse } from "./sse";

/** What the chat uses of the editor's `RevisionResult`, the `result` event of a turn. */
export interface RevisionResult {
  message: string;
  /** Authoritative: replaces the text streamed during the turn. */
  reply: string;
  applied: boolean;
  summary: string | null;
  notes_changed: boolean;
  /** Anchors of the sections the turn's edits touched. */
  changed_sections: string[];
  /** Unified diff of `notes/apuntes.md`, `""` when the notes did not change. */
  diff: string;
  fidelity_mode: string | null;
  style_rules: string[];
  commit: string | null;
  warning: string | null;
}

export interface ChatTurn {
  time: string;
  message: string;
  reply: string;
  applied: boolean;
  summary: string | null;
  changed_sections: string[];
  commit: string | null;
  undone: boolean;
  warning: string | null;
}

export interface ChatHistory {
  turns: ChatTurn[];
  can_undo: boolean;
}

export interface UndoResult {
  undone_commit: string;
  summary: string | null;
  commit: string | null;
  notes_changed: boolean;
  diff: string;
}

/** A turn's outcome; `interrupted` when the stream ended before its `result` or `error`. */
export type ChatOutcome = ActionResult<RevisionResult> | { kind: "interrupted" };

export interface StreamHandlers {
  /** A piece of the editor's reply, as it is written. */
  onDelta?: (text: string, attempt: number) => void;
  /** The change was sent back to the editor: drop the reply streamed so far. */
  onRestart?: (attempt: number) => void;
}

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
const optionalText = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const flag = (value: unknown): boolean => value === true;

export function readRevision(body: unknown): RevisionResult | null {
  if (!isObject(body) || typeof body.reply !== "string" || typeof body.applied !== "boolean") return null;
  return {
    message: text(body.message),
    reply: body.reply,
    applied: body.applied,
    summary: optionalText(body.summary),
    notes_changed: flag(body.notes_changed),
    changed_sections: strings(body.changed_sections),
    diff: text(body.diff),
    fidelity_mode: optionalText(body.fidelity_mode),
    style_rules: strings(body.style_rules),
    commit: optionalText(body.commit),
    warning: optionalText(body.warning),
  };
}

function readTurn(body: unknown): ChatTurn | null {
  if (!isObject(body) || typeof body.message !== "string" || typeof body.reply !== "string") return null;
  return {
    time: text(body.time),
    message: body.message,
    reply: body.reply,
    applied: flag(body.applied),
    summary: optionalText(body.summary),
    changed_sections: strings(body.changed_sections),
    commit: optionalText(body.commit),
    undone: flag(body.undone),
    warning: optionalText(body.warning),
  };
}

export function readHistory(body: unknown): ChatHistory | null {
  if (!isObject(body) || !Array.isArray(body.turns)) return null;
  const turns = body.turns.map(readTurn);
  if (turns.some((turn) => turn === null)) return null;
  return { turns: turns as ChatTurn[], can_undo: flag(body.can_undo) };
}

export function readUndo(body: unknown): UndoResult | null {
  if (!isObject(body) || typeof body.undone_commit !== "string") return null;
  return {
    undone_commit: body.undone_commit,
    summary: optionalText(body.summary),
    commit: optionalText(body.commit),
    notes_changed: flag(body.notes_changed),
    diff: text(body.diff),
  };
}

export function chatPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/notes/chat`;
}

/** `GET .../notes/chat`: the conversation with the editor, oldest turn first. */
export async function fetchChatHistory(subjectId: string, topicId: string): Promise<ReadResult<ChatHistory>> {
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
  const history = response.ok ? readHistory(body) : null;
  return history === null ? { kind: "error", status: response.status } : { kind: "ok", value: history };
}

/** `POST .../notes/chat/undo`: reverts the latest applied turn not yet undone. */
export function undoLastTurn(subjectId: string, topicId: string): Promise<ActionResult<UndoResult>> {
  return postAction(`${chatPath(subjectId, topicId)}/undo`, undefined, readUndo);
}

function parseData(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

/**
 * `POST .../notes/chat`: sends one message and reads the turn's stream. Errors before the stream
 * (404, 409 busy or no notes, 422, 503) come back as `refused` with the backend's Spanish `detail`;
 * an `error` event inside the stream as `refused` with its own status (a reached cost cap is
 * `overCap` by its `code` `cost_cap_reached`, so the caller can repeat the message with
 * `confirmOverCap`).
 */
export async function sendChatMessage(
  subjectId: string,
  topicId: string,
  message: string,
  { confirmOverCap = false, ...handlers }: StreamHandlers & { confirmOverCap?: boolean } = {},
): Promise<ChatOutcome> {
  let response: Response;
  try {
    response = await fetch(chatPath(subjectId, topicId), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ message, confirm_over_cap: confirmOverCap }),
    });
  } catch {
    return { kind: "unreachable" };
  }
  if (!response.ok || response.body === null) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const detail = isObject(body) ? optionalText(body.detail) : null;
    if (detail === null) return { kind: "error", status: response.status };
    const code = errorCode(body);
    return { kind: "refused", status: response.status, detail, code, overCap: isOverCap(code) };
  }

  let outcome: ChatOutcome | null = null;
  try {
    await readSse(response.body, ({ event, data }) => {
      if (outcome !== null) return;
      const body = parseData(data);
      const payload = isObject(body) ? body : {};
      const attempt = typeof payload.attempt === "number" ? payload.attempt : 1;
      if (event === "reply.delta") {
        handlers.onDelta?.(text(payload.text), attempt);
      } else if (event === "reply.restart") {
        handlers.onRestart?.(attempt);
      } else if (event === "result") {
        const value = readRevision(body);
        outcome = value === null ? { kind: "error", status: response.status } : { kind: "ok", value };
      } else if (event === "error") {
        const status = typeof payload.status === "number" ? payload.status : 500;
        const detail = optionalText(payload.detail);
        const code = errorCode(payload);
        outcome =
          detail === null
            ? { kind: "error", status }
            : { kind: "refused", status, detail, code, overCap: isOverCap(code) };
      }
    });
  } catch {
    return outcome ?? { kind: "interrupted" };
  }
  return outcome ?? { kind: "interrupted" };
}

export function describeChatFailure(result: Exclude<ChatOutcome, { kind: "ok" }>): string {
  switch (result.kind) {
    case "refused":
      return result.detail;
    case "error":
      return `El servidor respondió con un error (${result.status}).`;
    case "unreachable":
      return "No se pudo conectar con el servidor.";
    case "interrupted":
      return "Se cortó la conexión con el editor antes de terminar; los apuntes muestran lo que quedó guardado.";
  }
}
