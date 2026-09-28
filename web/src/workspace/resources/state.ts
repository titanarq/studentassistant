/**
 * The state of each source of the **Recursos** tab (#328, epic #311 "Shared design"):
 * - **Pendiente**: kept (no sidecar `triage`, or `triage.status` `kept`/`flagged`) and not cited
 *   by the current notes;
 * - **Incorporada**: kept and cited by the current notes (a provenance footnote definition links
 *   it), so it follows every `notes.changed` without another request;
 * - **Apartada**: the sidecar's `triage.status` is `set_aside` (#324), with its reasons.
 * The triage is read from `GET /api/sources/{id}/meta` (`meta.triage`, docs/modules/sources.md).
 */

import type { SourceMeta } from "../../notes/api";
import type { NotesTree } from "../../notes/markdown";
import { parseProvenance, sourceVaultId, stemOf } from "../../notes/provenance";
import type { ResourceItem } from "../resources";

export type SourceKind = "notes" | "book" | "pdf" | "web" | "images";

export type SourceState = "pending" | "incorporated" | "set_aside";

/** A stored source of the topic: its kind and file under `sources/<kind>/`. */
export interface SourceRef {
  kind: SourceKind;
  file: string;
}

/** The sidecar's `triage` block, as far as the tab needs it. */
export interface Triage {
  status: "kept" | "flagged" | "set_aside";
  reasons: string[];
  /** Topic-relative id of the page it repeats (`sources/notes/page-003.jpg`), or null. */
  duplicateOf: string | null;
  decidedBy: string | null;
}

/** One source as the tab shows it. */
export interface SourceEntry {
  key: string;
  /** What `SourcePanel` opens: a footnote-like label and definition. */
  label: string;
  definition: string;
  ref: SourceRef;
  vaultId: string;
  title: string;
  state: SourceState;
  /** The Spanish reasons of a set-aside source, or the warning of a flagged one. */
  reasons: string[];
  flagged: boolean;
  /** The student set it aside (`decided_by: student`). */
  byStudent: boolean;
}

export interface SourceCountsByState {
  pending: number;
  incorporated: number;
  setAside: number;
}

export interface ResourceStates {
  /** Kept sources (pending and incorporated) in source order. */
  kept: SourceEntry[];
  /** Set-aside sources in source order. */
  setAside: SourceEntry[];
  counts: SourceCountsByState;
  /** Listed items that are not stored sources (cited transcript spans): no state. */
  others: ResourceItem[];
}

/** The stored source a footnote-like definition links, or null (transcript, `[^ia]`, unknown). */
export function sourceRefOf(label: string, definition: string): SourceRef | null {
  const provenance = parseProvenance(label, definition);
  switch (provenance.kind) {
    case "page":
      return { kind: provenance.sourceKind, file: provenance.file };
    case "pdf":
      return { kind: "pdf", file: provenance.file };
    case "web":
      return { kind: "web", file: provenance.file };
    case "image":
      return { kind: "images", file: provenance.file };
    default:
      return null;
  }
}

export function refKey(ref: SourceRef): string {
  return `${ref.kind}/${ref.file}`;
}

/** The keys (`<kind>/<file>`) of the sources the notes' footnote definitions link. */
export function citedSources(tree: NotesTree | null): Set<string> {
  const cited = new Set<string>();
  for (const footnote of tree?.footnotes ?? []) {
    const ref = sourceRefOf(footnote.label, footnote.text);
    if (ref !== null) cited.add(refKey(ref));
  }
  return cited;
}

const STATUSES = new Set(["kept", "flagged", "set_aside"]);

/** The sidecar's `triage` block, or null when it has none (stored before #324, not a capture). */
export function triageOf(sidecar: Record<string, unknown> | null | undefined): Triage | null {
  const triage = sidecar?.triage;
  if (typeof triage !== "object" || triage === null || Array.isArray(triage)) return null;
  const block = triage as Record<string, unknown>;
  if (typeof block.status !== "string" || !STATUSES.has(block.status)) return null;
  return {
    status: block.status as Triage["status"],
    reasons: Array.isArray(block.reasons) ? block.reasons.filter((r): r is string => typeof r === "string") : [],
    duplicateOf: typeof block.duplicate_of === "string" ? block.duplicate_of : null,
    decidedBy: typeof block.decided_by === "string" ? block.decided_by : null,
  };
}

/** The number of `page-003.jpg`, `img-003.png` or `003-slug.md`, or null. */
export function sourceNumber(file: string): number | null {
  const match = /^(?:page-|img-)?(\d+)(?:[.-]|$)/.exec(file);
  return match ? Number(match[1]) : null;
}

function pageOf(sourceId: string | null): number | null {
  if (sourceId === null) return null;
  return sourceNumber(sourceId.slice(sourceId.lastIndexOf("/") + 1));
}

/** A triage reason in Spanish (docs/modules/sources.md, `TriageResult.reasons`). */
export function reasonText(reason: string, duplicateOf: string | null): string {
  const page = pageOf(duplicateOf);
  switch (reason) {
    case "blank":
      return "En blanco";
    case "duplicate":
      return page === null ? "Repetida de otra página" : `Repetida de la página ${page}`;
    case "blurry":
      return "Borrosa";
    case "partial":
      return "Puede estar cortada";
    case "same_content":
      return page === null ? "Mismo contenido que otra página" : `Mismo contenido que la página ${page}`;
    default:
      return reason;
  }
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/**
 * "Página 3 · apuntes", "Libro, página 83", "PDF «tema2.pdf»", "Web: …", "Imagen pegada 1", and
 * "Imagen recortada 2" for a region the editor cropped from a page (sidecar `origin: cropped`, #493).
 */
export function sourceTitle(ref: SourceRef, fallback: string, sidecar: Record<string, unknown> | null): string {
  const n = sourceNumber(ref.file);
  switch (ref.kind) {
    case "notes":
      return n === null ? `${fallback} · apuntes` : `Página ${n} · apuntes`;
    case "book":
      return n === null ? fallback : `Libro, página ${n}`;
    case "pdf": {
      const name = text(sidecar?.original_name);
      return name === null ? "PDF" : `PDF «${name}»`;
    }
    case "web": {
      const title = text(sidecar?.title) ?? fallback.replace(/^Web:\s*/, "");
      return `Web: ${title}`;
    }
    case "images": {
      const name = sidecar?.origin === "cropped" ? "Imagen recortada" : "Imagen pegada";
      return n === null ? name : `${name} ${n}`;
    }
  }
}

/** The thumbnail of a source: an image to show and one to fall back to, or null (a web). */
export function thumbnailOf(vaultId: string, ref: SourceRef): { src: string; fallback: string | null } | null {
  const dir = vaultId.slice(0, vaultId.lastIndexOf("/") + 1);
  const stem = stemOf(ref.file);
  switch (ref.kind) {
    case "notes":
    case "book":
      return { src: `${dir}${stem}.page.jpg`, fallback: vaultId };
    case "pdf":
      return { src: `${dir}${stem}.p001.jpg`, fallback: null };
    case "images":
      return { src: vaultId, fallback: null };
    default:
      return null;
  }
}

/**
 * Whether a source was retired by the student (#451, a soft delete): the meta's `removed`, or a
 * `removed` mapping in its sidecar. Such a source stays out of the tab even when the notes still
 * cite it (#456); the citation itself keeps opening it.
 */
export function isRemoved(meta: SourceMeta | null | undefined): boolean {
  if (meta === null || meta === undefined) return false;
  if (meta.removed === true) return true;
  const mark = meta.meta?.removed;
  return typeof mark === "object" && mark !== null && !Array.isArray(mark);
}

/**
 * The tab's entries, from the listed items (`resourceList`, in their order), the current notes
 * and the sources' metadata read so far (`undefined` while unread, `null` when it failed: kept).
 * One entry per stored source: the same PDF cited at several pages is one source.
 */
export function resourceStates(
  subjectId: string,
  topicId: string,
  items: ResourceItem[],
  tree: NotesTree | null,
  metas: ReadonlyMap<string, SourceMeta | null>,
): ResourceStates {
  const cited = citedSources(tree);
  const seen = new Set<string>();
  const kept: SourceEntry[] = [];
  const setAside: SourceEntry[] = [];
  const others: ResourceItem[] = [];
  for (const item of items) {
    const ref = sourceRefOf(item.label, item.definition);
    if (ref === null) {
      others.push(item);
      continue;
    }
    const key = refKey(ref);
    if (seen.has(key)) continue;
    seen.add(key);
    const vaultId = sourceVaultId(subjectId, topicId, ref.kind, ref.file);
    const read = metas.get(vaultId);
    // A retired source is never listed (#456); one only the notes cite waits for its metadata.
    if (isRemoved(read) || (item.unlisted === true && read === undefined)) continue;
    const sidecar = metas.get(vaultId)?.meta ?? null;
    const triage = triageOf(sidecar);
    const state: SourceState =
      triage?.status === "set_aside" ? "set_aside" : cited.has(key) ? "incorporated" : "pending";
    const flagged = triage?.status === "flagged";
    const reasons = state === "set_aside" || flagged ? (triage?.reasons ?? []).map((r) => reasonText(r, triage?.duplicateOf ?? null)) : [];
    const entry: SourceEntry = {
      key,
      label: item.label,
      definition: item.definition,
      ref,
      vaultId,
      title: sourceTitle(ref, item.title, sidecar),
      state,
      reasons,
      flagged,
      byStudent: state === "set_aside" && triage?.decidedBy === "student",
    };
    (state === "set_aside" ? setAside : kept).push(entry);
  }
  return {
    kept,
    setAside,
    counts: {
      pending: kept.filter((e) => e.state === "pending").length,
      incorporated: kept.filter((e) => e.state === "incorporated").length,
      setAside: setAside.length,
    },
    others,
  };
}
