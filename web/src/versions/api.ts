/**
 * Client of the notes versions API (#64, `docs/modules/server.md`):
 * - `GET /api/subjects/{s}/topics/{t}/notes/versions` -> `NotesVersions` (oldest first).
 * - `GET .../notes/versions/diff?from=K[&to=N]` -> `VersionDiff`, compared by section; without
 *   `to`, against the current `apuntes.md`.
 * - `POST .../notes/versions/{K}/restore` -> `RestoreResult`: version K written back as the next
 *   version (nothing is rewound).
 *
 * Every call answers an `ActionResult` (`pending/doubts.ts`): a refusal keeps the backend's Spanish
 * `detail` (404 unknown topic or version, 409 nothing to compare with, already that version, or
 * another notes operation running). Bodies are read leniently: a field of the wrong type is
 * defaulted, a body without the fields the page needs is an error.
 */

import { topicPath } from "../desk/api";
import { errorCode } from "../protocol";
import { type ActionResult, isOverCap, postAction } from "../pending/doubts";

export interface NotesVersion {
  version: number;
  tag: string;
  commit: string;
  /** ISO 8601, `null` when git does not say. */
  tagged_at: string | null;
  message: string;
  /** The current `apuntes.md` is exactly this version. */
  current: boolean;
}

export interface NotesVersions {
  /** Oldest first. */
  versions: NotesVersion[];
  has_notes: boolean;
  /** The current notes differ from the latest version (a revision, a doubt's edit). */
  changed_since_latest: boolean;
}

export type SectionStatus = "added" | "removed" | "changed" | "unchanged";

export interface SectionDiff {
  /** The anchor, or `""` for the preamble. */
  key: string;
  anchor: string | null;
  title_before: string | null;
  title_after: string | null;
  level: number;
  status: SectionStatus;
  moved: boolean;
  renamed: boolean;
  /** Unified diff of the section's heading and blocks. */
  diff: string;
}

export interface FootnotesDiff {
  added: string[];
  removed: string[];
  changed: string[];
}

export interface VersionDiff {
  from_version: number;
  /** `null`: the current notes. */
  to_version: number | null;
  identical: boolean;
  sections: SectionDiff[];
  footnotes: FootnotesDiff;
}

export interface RestoreResult {
  restored_version: number;
  /** The new version the restore made. */
  version: number;
  errors: string[];
  warning: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function positive(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : null;
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function readVersions(body: unknown): NotesVersions | null {
  if (!isRecord(body) || !Array.isArray(body.versions)) return null;
  const versions: NotesVersion[] = [];
  for (const entry of body.versions) {
    if (!isRecord(entry)) continue;
    const version = positive(entry.version);
    if (version === null) continue;
    versions.push({
      version,
      tag: text(entry.tag),
      commit: text(entry.commit),
      tagged_at: nullableText(entry.tagged_at),
      message: text(entry.message),
      current: entry.current === true,
    });
  }
  versions.sort((a, b) => a.version - b.version);
  return {
    versions,
    has_notes: body.has_notes === true,
    changed_since_latest: body.changed_since_latest === true,
  };
}

const STATUSES: readonly SectionStatus[] = ["added", "removed", "changed", "unchanged"];

function readSection(entry: unknown): SectionDiff | null {
  if (!isRecord(entry) || typeof entry.key !== "string") return null;
  const status = STATUSES.find((s) => s === entry.status);
  if (status === undefined) return null;
  return {
    key: entry.key,
    anchor: nullableText(entry.anchor),
    title_before: nullableText(entry.title_before),
    title_after: nullableText(entry.title_after),
    level: typeof entry.level === "number" ? entry.level : 0,
    status,
    moved: entry.moved === true,
    renamed: entry.renamed === true,
    diff: text(entry.diff),
  };
}

export function readDiff(body: unknown): VersionDiff | null {
  if (!isRecord(body) || !Array.isArray(body.sections)) return null;
  const from = positive(body.from_version);
  if (from === null) return null;
  const footnotes = isRecord(body.footnotes) ? body.footnotes : {};
  return {
    from_version: from,
    to_version: positive(body.to_version),
    identical: body.identical === true,
    sections: body.sections.map(readSection).filter((s): s is SectionDiff => s !== null),
    footnotes: { added: texts(footnotes.added), removed: texts(footnotes.removed), changed: texts(footnotes.changed) },
  };
}

export function readRestore(body: unknown): RestoreResult | null {
  if (!isRecord(body)) return null;
  const restored = positive(body.restored_version);
  const version = positive(body.version);
  if (restored === null || version === null) return null;
  return { restored_version: restored, version, errors: texts(body.errors), warning: nullableText(body.warning) };
}

export function versionsPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/notes/versions`;
}

async function getAction<T>(path: string, read: (body: unknown) => T | null): Promise<ActionResult<T>> {
  let response: Response;
  try {
    response = await fetch(path);
  } catch {
    return { kind: "unreachable" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    const detail = isRecord(body) ? body.detail : undefined;
    if (typeof detail === "string" && detail !== "") {
      const code = errorCode(body);
      return { kind: "refused", status: response.status, detail, code, overCap: isOverCap(code) };
    }
    return { kind: "error", status: response.status };
  }
  const value = read(body);
  return value === null ? { kind: "error", status: response.status } : { kind: "ok", value };
}

export function fetchVersions(subjectId: string, topicId: string): Promise<ActionResult<NotesVersions>> {
  return getAction(versionsPath(subjectId, topicId), readVersions);
}

/** `to` `null` compares with the current notes. */
export function fetchVersionDiff(
  subjectId: string,
  topicId: string,
  from: number,
  to: number | null,
): Promise<ActionResult<VersionDiff>> {
  const query = new URLSearchParams({ from: String(from) });
  if (to !== null) query.set("to", String(to));
  return getAction(`${versionsPath(subjectId, topicId)}/diff?${query}`, readDiff);
}

export function restoreVersion(subjectId: string, topicId: string, version: number): Promise<ActionResult<RestoreResult>> {
  return postAction(`${versionsPath(subjectId, topicId)}/${version}/restore`, undefined, readRestore);
}
