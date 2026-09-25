/**
 * Client of the study materials API (#67, `docs/modules/server.md`, "Study materials"):
 * - `GET /api/generators` -> every registered generator (`kind`, Spanish `title`, `description`).
 * - `GET /api/subjects/{s}/topics/{t}/generated` -> `MaterialsStatus`: per kind whether it was
 *   generated, whether it is stale (and the Spanish reason), its files and its manifest.
 * - `POST .../generated/{kind}`, body `{options, confirm_over_cap}` -> `GenerateResult`.
 * - `GET .../generated/files/{name}` -> the bytes of one generated file (a download; the preview
 *   reads a Markdown one as text).
 *
 * Bodies are read leniently: an entry the page cannot show is skipped, a body without the fields
 * the page needs is an error. Writes answer an `ActionResult` (`pending/doubts.ts`), reads a
 * `ReadResult` (`desk/api.ts`); a refusal keeps the backend's Spanish `detail`.
 */

import { type ReadResult, topicPath } from "../desk/api";
import { type ActionResult, postAction } from "../pending/doubts";

export interface GeneratorInfo {
  kind: string;
  title: string;
  description: string;
}

export interface Artifact {
  kind: string;
  /** Null for a kind no longer registered. */
  title: string | null;
  generated: boolean;
  stale: boolean;
  staleReason: string | null;
  /** Names relative to `generated/` (`esquema.md`, `diapositivas/figura-01.jpg`...). */
  files: string[];
  /** ISO 8601, from the manifest. */
  builtAt: string | null;
  notesVersion: number | null;
  warnings: string[];
}

export interface Materials {
  hasNotes: boolean;
  artifacts: Artifact[];
}

export interface Generated {
  kind: string;
  notesVersion: number | null;
  warnings: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function version(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : null;
}

/** The name under `generated/` of a vault-relative path, or null when it is not one. */
export function generatedName(path: string): string | null {
  const at = path.indexOf("/generated/");
  if (at >= 0) return path.slice(at + "/generated/".length) || null;
  return path.startsWith("generated/") ? path.slice("generated/".length) || null : null;
}

export function readGenerators(body: unknown): GeneratorInfo[] | null {
  if (!Array.isArray(body)) return null;
  return body.flatMap((entry) =>
    isRecord(entry) && typeof entry.kind === "string" && entry.kind !== "" && typeof entry.title === "string"
      ? [{ kind: entry.kind, title: entry.title, description: typeof entry.description === "string" ? entry.description : "" }]
      : [],
  );
}

function readArtifact(entry: unknown): Artifact | null {
  if (!isRecord(entry) || typeof entry.kind !== "string" || entry.kind === "") return null;
  const meta = isRecord(entry.meta) ? entry.meta : {};
  const notes = isRecord(meta.notes) ? meta.notes : {};
  return {
    kind: entry.kind,
    title: typeof entry.title === "string" ? entry.title : null,
    generated: entry.generated === true,
    stale: entry.stale === true,
    staleReason: typeof entry.stale_reason === "string" && entry.stale_reason !== "" ? entry.stale_reason : null,
    files: texts(entry.files)
      .map(generatedName)
      .filter((name): name is string => name !== null && !name.endsWith(".meta.yaml")),
    builtAt: typeof meta.built_at === "string" ? meta.built_at : null,
    notesVersion: version(notes.version),
    warnings: texts(meta.warnings),
  };
}

export function readMaterials(body: unknown): Materials | null {
  if (!isRecord(body) || typeof body.has_notes !== "boolean" || !Array.isArray(body.artifacts)) return null;
  return {
    hasNotes: body.has_notes,
    artifacts: body.artifacts.map(readArtifact).filter((a): a is Artifact => a !== null),
  };
}

export function readGenerated(body: unknown): Generated | null {
  if (!isRecord(body) || typeof body.kind !== "string") return null;
  const notes = isRecord(body.notes) ? body.notes : {};
  return { kind: body.kind, notesVersion: version(notes.version), warnings: texts(body.warnings) };
}

export function generatedPath(subjectId: string, topicId: string): string {
  return `/api${topicPath(subjectId, topicId)}/generated`;
}

/** The download URL of a file under `generated/` (subdirectories kept, each segment encoded). */
export function fileUrl(subjectId: string, topicId: string, name: string): string {
  return `${generatedPath(subjectId, topicId)}/files/${name.split("/").map(encodeURIComponent).join("/")}`;
}

/** The web page previewing a generated Markdown file. */
export function previewPath(subjectId: string, topicId: string, name: string): string {
  return `${topicPath(subjectId, topicId)}/material/${encodeURIComponent(name)}`;
}

async function get(path: string): Promise<{ response: Response } | { failure: ReadResult<never> }> {
  try {
    return { response: await fetch(path) };
  } catch {
    return { failure: { kind: "unreachable" } };
  }
}

async function detailOf(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === "string" && body.detail !== "") return body.detail;
  } catch {
    // no JSON body
  }
  return "No encontrado.";
}

async function getJson<T>(path: string, read: (body: unknown) => T | null): Promise<ReadResult<T>> {
  const answer = await get(path);
  if ("failure" in answer) return answer.failure;
  const { response } = answer;
  if (response.status === 404) return { kind: "not-found", detail: await detailOf(response) };
  if (!response.ok) return { kind: "error", status: response.status };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "error", status: response.status };
  }
  const value = read(body);
  return value === null ? { kind: "error", status: response.status } : { kind: "ok", value };
}

export function fetchGenerators(): Promise<ReadResult<GeneratorInfo[]>> {
  return getJson("/api/generators", readGenerators);
}

export function fetchMaterials(subjectId: string, topicId: string): Promise<ReadResult<Materials>> {
  return getJson(generatedPath(subjectId, topicId), readMaterials);
}

/** A generated file as text (the preview of a Markdown one). */
export async function fetchGeneratedText(subjectId: string, topicId: string, name: string): Promise<ReadResult<string>> {
  const answer = await get(fileUrl(subjectId, topicId, name));
  if ("failure" in answer) return answer.failure;
  const { response } = answer;
  if (response.status === 404) return { kind: "not-found", detail: await detailOf(response) };
  if (!response.ok) return { kind: "error", status: response.status };
  try {
    return { kind: "ok", value: await response.text() };
  } catch {
    return { kind: "error", status: response.status };
  }
}

export function generateMaterial(
  subjectId: string,
  topicId: string,
  kind: string,
  confirmOverCap = false,
): Promise<ActionResult<Generated>> {
  return postAction(
    `${generatedPath(subjectId, topicId)}/${encodeURIComponent(kind)}`,
    { options: {}, confirm_over_cap: confirmOverCap },
    readGenerated,
  );
}
