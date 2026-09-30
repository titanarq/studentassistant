/**
 * The open doubts marked in the notes (#516): where each one goes (`GET .../doubts/marks`,
 * `editor.doubt_marks`) and showing one in the chat (`POST .../doubts/{id}/ask`). Doubts are never
 * written into the notes: the viewer draws a «?» badge in the margin of the blocks that cite a
 * doubt's sources, on its section's heading, or in a header at the top of the notes.
 *
 * Blocks are addressed as `NotesView` numbers them (the "¿Por qué?" numbering): the anchor of
 * their section (`null` before the first one) and their number in it, from 1.
 */

import { type ReadResult } from "../desk/api";
import { type ActionResult, doubtsPath, postAction } from "../pending/doubts";

type Json = Record<string, unknown>;
const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string => (typeof value === "string" ? value : "");

export interface MarkedBlock {
  section: string | null;
  number: number;
}

export interface DoubtMark {
  pendingId: string;
  kind: string;
  text: string;
  level: "block" | "section" | "top";
  blocks: MarkedBlock[];
  section: string | null;
  /** Asked in the chat and not answered yet. */
  asked: boolean;
}

/** The marks in the order of the notes (the top ones first). */
export interface DoubtMarks {
  count: number;
  marks: DoubtMark[];
}

/** Where the badges go: pending ids per block (`blockKey`), per section heading, and at the top. */
export interface DoubtBadges {
  blocks: ReadonlyMap<string, string[]>;
  sections: ReadonlyMap<string, string[]>;
  top: string[];
}

export const NO_BADGES: DoubtBadges = { blocks: new Map(), sections: new Map(), top: [] };

export const blockKey = (section: string | null, number: number): string => `${section ?? ""}#${number}`;

function readBlock(value: unknown): MarkedBlock[] {
  if (!isObject(value) || typeof value.number !== "number" || value.number < 1) return [];
  return [{ section: typeof value.section === "string" ? value.section : null, number: value.number }];
}

function readMark(value: unknown): DoubtMark[] {
  if (!isObject(value) || typeof value.pending_id !== "string" || value.pending_id === "") return [];
  const blocks = Array.isArray(value.blocks) ? value.blocks.flatMap(readBlock) : [];
  const section = typeof value.section === "string" && value.section !== "" ? value.section : null;
  const level = blocks.length > 0 ? "block" : section !== null ? "section" : "top";
  return [
    {
      pendingId: value.pending_id,
      kind: text(value.kind),
      text: text(value.text),
      level,
      blocks,
      section,
      asked: value.asked === true,
    },
  ];
}

/** A `DoubtMarks` body read leniently; `null` when it is not one. */
export function readDoubtMarks(body: unknown): DoubtMarks | null {
  if (!isObject(body) || !Array.isArray(body.marks)) return null;
  const marks = body.marks.flatMap(readMark);
  return { count: marks.length, marks };
}

export function badgesOf(marks: DoubtMarks | null): DoubtBadges {
  if (marks === null || marks.marks.length === 0) return NO_BADGES;
  const blocks = new Map<string, string[]>();
  const sections = new Map<string, string[]>();
  const top: string[] = [];
  const add = (map: Map<string, string[]>, key: string, id: string) => {
    const ids = map.get(key) ?? [];
    if (!ids.includes(id)) map.set(key, [...ids, id]);
  };
  for (const mark of marks.marks) {
    if (mark.level === "block") mark.blocks.forEach((b) => add(blocks, blockKey(b.section, b.number), mark.pendingId));
    else if (mark.level === "section" && mark.section !== null) add(sections, mark.section, mark.pendingId);
    else top.push(mark.pendingId);
  }
  return { blocks, sections, top };
}

/**
 * Which of `ids` (a badge's doubts, in the order of the notes) to show: the first not asked in the
 * chat yet, else the first. `null` for none.
 */
export function pickDoubt(marks: DoubtMarks | null, ids: readonly string[]): string | null {
  if (ids.length === 0) return null;
  const asked = new Set(marks?.marks.filter((m) => m.asked).map((m) => m.pendingId) ?? []);
  return ids.find((id) => !asked.has(id)) ?? ids[0];
}

/** «Siguiente duda.»: the first marked doubt, in the order of the notes, not asked yet. */
export function nextDoubt(marks: DoubtMarks | null): string | null {
  return pickDoubt(marks, marks?.marks.map((m) => m.pendingId) ?? []);
}

export function doubtsLine(count: number): string {
  return count === 1 ? "Tienes 1 duda marcada en los apuntes" : `Tienes ${count} dudas marcadas en los apuntes`;
}

export async function fetchDoubtMarks(subjectId: string, topicId: string): Promise<ReadResult<DoubtMarks>> {
  let response: Response;
  try {
    response = await fetch(`${doubtsPath(subjectId, topicId)}/marks`);
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
    const detail = isObject(body) && typeof body.detail === "string" && body.detail !== "" ? body.detail : "No encontrado.";
    return { kind: "not-found", detail };
  }
  const marks = response.ok ? readDoubtMarks(body) : null;
  return marks === null ? { kind: "error", status: response.status } : { kind: "ok", value: marks };
}

/** What showing a doubt did: asked in the chat, or settled from the sources by its review. */
export interface ShownDoubt {
  pendingId: string;
  asked: boolean;
  summary: string | null;
}

function readShown(body: unknown): ShownDoubt | null {
  if (!isObject(body) || typeof body.pending_id !== "string" || typeof body.asked !== "boolean") return null;
  return { pendingId: body.pending_id, asked: body.asked, summary: typeof body.summary === "string" ? body.summary : null };
}

export function askDoubt(subjectId: string, topicId: string, pendingId: string): Promise<ActionResult<ShownDoubt>> {
  return postAction(`${doubtsPath(subjectId, topicId)}/${encodeURIComponent(pendingId)}/ask`, undefined, readShown);
}
