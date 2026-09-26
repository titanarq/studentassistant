/**
 * What the **Recursos** tab lists (#312): the topic's sources, from the counts of the topic
 * summary (`GET .../summary`, what the topic page shows) and the provenance footnotes of the
 * notes. The read API has no listing of a topic's source files, so:
 * - handwritten and book pages and PDFs are numbered `page-001` upwards (`sources/notes/*.jpg`,
 *   `sources/book/*.jpg`, `sources/pdf/*.pdf`, docs/modules/vault.md), one per counted source;
 * - web snapshots are named after their title (`NNN-<slug>.md`), so only the ones the notes cite
 *   can be opened; the rest are counted (`uncitedWebs`);
 * - transcript spans the notes cite are listed too.
 * Every item carries a footnote-like `label` and `definition`, which is what `SourcePanel` opens.
 */

import type { SourceCounts } from "../desk/api";
import type { NotesTree } from "../notes/markdown";
import { parseProvenance, type Provenance } from "../notes/provenance";

export type ResourceGroupKind = "notes" | "book" | "pdf" | "web" | "transcript";

export interface ResourceItem {
  key: string;
  label: string;
  definition: string;
  title: string;
}

export interface ResourceGroup {
  kind: ResourceGroupKind;
  title: string;
  items: ResourceItem[];
}

export interface ResourceList {
  groups: ResourceGroup[];
  /** Web snapshots counted by the summary that the notes do not cite (not openable here). */
  uncitedWebs: number;
}

export const GROUP_TITLES: Record<ResourceGroupKind, string> = {
  notes: "Páginas de apuntes",
  book: "Páginas del libro",
  pdf: "PDF",
  web: "Webs",
  transcript: "Fragmentos de la transcripción",
};

const ORDER: ResourceGroupKind[] = ["notes", "book", "pdf", "web", "transcript"];

function pageFile(n: number, ext: string): string {
  return `page-${String(n).padStart(3, "0")}.${ext}`;
}

function keyOf(provenance: Provenance): { group: ResourceGroupKind; key: string } | null {
  switch (provenance.kind) {
    case "page":
      return { group: provenance.sourceKind, key: `${provenance.sourceKind}/${provenance.file}` };
    case "pdf":
      return { group: "pdf", key: `pdf/${provenance.file}#${provenance.page ?? ""}` };
    case "web":
      return { group: "web", key: `web/${provenance.file}` };
    case "transcript":
      return { group: "transcript", key: `transcript/${provenance.sessionId}#${provenance.span}` };
    default:
      return null;
  }
}

export function resourceList(counts: SourceCounts | null, tree: NotesTree | null): ResourceList {
  const groups = new Map<ResourceGroupKind, Map<string, ResourceItem>>(ORDER.map((kind) => [kind, new Map()]));
  const add = (group: ResourceGroupKind, item: ResourceItem) => {
    const items = groups.get(group)!;
    if (!items.has(item.key)) items.set(item.key, item);
  };

  const derived: Array<[ResourceGroupKind, string, string, (n: number) => string]> = [
    ["notes", "notes", "jpg", (n) => `Apuntes, página ${n}`],
    ["book", "book", "jpg", (n) => `Libro, página ${n}`],
    ["pdf", "pdf", "pdf", (n) => `PDF ${n}`],
  ];
  for (const [group, kind, ext, title] of derived) {
    const count = counts?.[kind as keyof SourceCounts] ?? 0;
    for (let n = 1; n <= count; n++) {
      const file = pageFile(n, ext);
      const key = kind === "pdf" ? `pdf/${file}#` : `${kind}/${file}`;
      add(group, { key, label: `recurso-${kind}-${n}`, definition: `[${title(n)}](../sources/${kind}/${file})`, title: title(n) });
    }
  }

  const citedWebs = new Set<string>();
  for (const footnote of tree?.footnotes ?? []) {
    const provenance = parseProvenance(footnote.label, footnote.text);
    const where = keyOf(provenance);
    if (where === null) continue;
    if (where.group === "web") citedWebs.add(where.key);
    add(where.group, { key: where.key, label: footnote.label, definition: footnote.text, title: provenance.text });
  }

  return {
    groups: ORDER.map((kind) => ({ kind, title: GROUP_TITLES[kind], items: [...groups.get(kind)!.values()] })).filter(
      (group) => group.items.length > 0,
    ),
    uncitedWebs: Math.max(0, (counts?.web ?? 0) - citedWebs.size),
  };
}
