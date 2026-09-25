/**
 * Client of the subject style guide API (`server/style_guide_routes.py`, #70): the student's
 * general preferences for a subject, one rule per line. `GET .../style-guide` reads them, `POST
 * .../style-guide/rules` confirms rules the editor proposed in the chat and `PUT .../style-guide`
 * writes the whole list (an edit, a deletion or a new rule). Bodies are read leniently.
 */

import type { ReadResult } from "../desk/api";
import { type ActionResult, isOverCap } from "../pending/doubts";
import { errorCode } from "../protocol";

/** The backend's `StyleGuide`: the rules now, plus what a change added and its commit. */
export interface StyleGuide {
  subject: string;
  rules: string[];
  added: string[];
  commit: string | null;
}

/** `MAX_RULE_CHARS` of `editor.style_guide`: a longer rule is refused. */
export const MAX_RULE_CHARS = 300;
/** `MAX_RULES` of `editor.style_guide`: the most rules a guide may hold. */
export const MAX_RULES = 50;

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function readStyleGuide(body: unknown): StyleGuide | null {
  if (!isObject(body) || !Array.isArray(body.rules)) return null;
  return {
    subject: typeof body.subject === "string" ? body.subject : "",
    rules: strings(body.rules),
    added: strings(body.added),
    commit: typeof body.commit === "string" && body.commit !== "" ? body.commit : null,
  };
}

/** The web page of a subject's style guide. */
export function styleGuidePagePath(subjectId: string): string {
  return `/subjects/${encodeURIComponent(subjectId)}/style-guide`;
}

function apiPath(subjectId: string): string {
  return `/api${styleGuidePagePath(subjectId)}`;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/** `GET /api/subjects/{s}/style-guide`: a 404 is an unknown subject, with its Spanish detail. */
export async function fetchStyleGuide(subjectId: string): Promise<ReadResult<StyleGuide>> {
  let response: Response;
  try {
    response = await fetch(apiPath(subjectId));
  } catch {
    return { kind: "unreachable" };
  }
  const body = await readJson(response);
  if (response.status === 404) {
    const detail = isObject(body) && typeof body.detail === "string" ? body.detail : "No encontrado.";
    return { kind: "not-found", detail };
  }
  const guide = response.ok ? readStyleGuide(body) : null;
  return guide === null ? { kind: "error", status: response.status } : { kind: "ok", value: guide };
}

async function sendRules(method: "POST" | "PUT", path: string, rules: string[]): Promise<ActionResult<StyleGuide>> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules }),
    });
  } catch {
    return { kind: "unreachable" };
  }
  const body = await readJson(response);
  if (!response.ok) {
    const detail = isObject(body) ? body.detail : undefined;
    // A validation error of the request itself (FastAPI) has a list as `detail`: a plain error.
    if (typeof detail === "string" && detail !== "") {
      const code = errorCode(body);
      return { kind: "refused", status: response.status, detail, code, overCap: isOverCap(code) };
    }
    return { kind: "error", status: response.status };
  }
  const guide = readStyleGuide(body);
  return guide === null ? { kind: "error", status: response.status } : { kind: "ok", value: guide };
}

/** `POST .../style-guide/rules`: the student confirms rules the editor proposed; `added` lists the new ones. */
export function confirmStyleRules(subjectId: string, rules: string[]): Promise<ActionResult<StyleGuide>> {
  return sendRules("POST", `${apiPath(subjectId)}/rules`, rules);
}

/** `PUT .../style-guide`: the whole list, edited, with rules removed or added (`[]` clears it). */
export function saveStyleGuide(subjectId: string, rules: string[]): Promise<ActionResult<StyleGuide>> {
  return sendRules("PUT", apiPath(subjectId), rules);
}

/** The same rule as the backend compares them: spaces collapsed, case ignored. */
export function sameRule(a: string, b: string): boolean {
  const norm = (rule: string) =>
    rule
      .trim()
      .replace(/^[-*•](?:\s+|$)/, "")
      .replace(/\s+/g, " ")
      .trim()
      .toLocaleLowerCase("es");
  return norm(a) === norm(b);
}
