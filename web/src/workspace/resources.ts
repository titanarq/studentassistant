/**
 * What the **Recursos** tab lists (#312, #323): the topic's sources and the provenance footnotes
 * of the notes.
 * - With the topic's source list (`GET .../sources`, `fetchTopicSources`, #323), every stored
 *   source (`notes`, `book`, `pdf`, `web`, `images`) is listed by its real file, in the backend's
 *   order, uncited webs included.
 * - Without it (a backend older than #323), from the counts of the topic summary (`GET
 *   .../summary`): handwritten and book pages and PDFs are numbered `page-001` upwards
 *   (`sources/notes/*.jpg`, `sources/book/*.jpg`, `sources/pdf/*.pdf`, docs/modules/vault.md),
 *   one per counted source; web snapshots are named after their title (`NNN-<slug>.md`), so only
 *   the ones the notes cite can be opened, the rest are counted (`uncitedWebs`); images pasted
 *   into the notes (`sources/images/`, #316) are listed when the notes cite them.
 * - Cited sources the list lacks (PDF pages, transcript spans) follow.
 * Every item carries a footnote-like `label` and `definition`, which is what `SourcePanel` opens.
 */

import { getJson, topicPath, type ReadResult, type SourceCounts } from "../desk/api";
import { array, id, object, str, type Decoder } from "../protocol/decode";
import type { NotesTree } from "../notes/markdown";
import { parseProvenance, type Provenance } from "../notes/provenance";

/** One source of `GET /api/subjects/{s}/topics/{t}/sources` (`TopicSourceItem`). */
export interface ListedSource {
  /** Vault-relative path, `subjects/<s>/topics/<t>/sources/<kind>/<file>`. */
  vault_id: string;
  kind: string;
  title?: string | null;
}

/** `TopicSources` of `GET /api/subjects/{s}/topics/{t}/sources`. */
export interface TopicSources {
  subject_id: string;
  topic_id: string;
  sources: ListedSource[];
}

const nullableTitle: Decoder<string | null> = (value, path) => (value === null ? null : str()(value, path));

export const decodeTopicSources: Decoder<TopicSources> = object({
  subject_id: id,
  topic_id: id,
  sources: array(object({ vault_id: str({ minLength: 1 }), kind: str({ minLength: 1 }) }, { title: nullableTitle })),
});

export function fetchTopicSources(subjectId: string, topicId: string): Promise<ReadResult<TopicSources>> {
  return getJson(`/api${topicPath(subjectId, topicId)}/sources`, decodeTopicSources);
}

export type ResourceGroupKind = "notes" | "book" | "pdf" | "web" | "images" | "transcript";

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
  images: "Imágenes pegadas",
  transcript: "Fragmentos de la transcripción",
};

export const ORDER: ResourceGroupKind[] = ["notes", "book", "pdf", "web", "images", "transcript"];

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
    case "image":
      return { group: "images", key: `images/${provenance.file}` };
    case "transcript":
      return { group: "transcript", key: `transcript/${provenance.sessionId}#${provenance.span}` };
    default:
      return null;
  }
}

const PAGE_NUMBER = /^(?:page|img)-(\d+)\./;

/** A listed source as an item of its group; `null` for a kind the tab does not show. */
function listedItem(source: ListedSource): [ResourceGroupKind, ResourceItem] | null {
  const parts = source.vault_id.split("/");
  const kind = parts.at(-2);
  const file = parts.at(-1) ?? "";
  if (kind !== source.kind || file === "") return null;
  const match = PAGE_NUMBER.exec(file);
  const n = match === null ? null : Number(match[1]);
  const title = source.title?.trim() || null;
  let text: string;
  switch (kind) {
    case "notes":
      text = n !== null ? `Apuntes, página ${n}` : `Apuntes: ${file}`;
      break;
    case "book":
      text = n !== null ? `Libro, página ${n}` : `Libro: ${file}`;
      break;
    case "pdf":
      text = title ?? (n !== null ? `PDF ${n}` : `PDF: ${file}`);
      break;
    case "web":
      text = `Web: ${title ?? file}`;
      break;
    case "images":
      text = n !== null ? `Imagen pegada ${n}` : "Imagen pegada";
      break;
    default:
      return null;
  }
  const key = kind === "pdf" ? `pdf/${file}#` : `${kind}/${file}`;
  const definition = `[${text.replace(/[[\]]/g, "")}](../sources/${kind}/${file})`;
  return [kind, { key, label: `recurso-${kind}-${file}`, definition, title: text }];
}

/**
 * The tab's groups from the topic's source list (an array) or, as a fallback, the summary's
 * counts, plus the sources the notes cite.
 */
export function resourceList(sources: ListedSource[] | SourceCounts | null, tree: NotesTree | null): ResourceList {
  const listed = Array.isArray(sources) ? sources : null;
  const counts = Array.isArray(sources) ? null : sources;
  const groups = new Map<ResourceGroupKind, Map<string, ResourceItem>>(ORDER.map((kind) => [kind, new Map()]));
  const add = (group: ResourceGroupKind, item: ResourceItem) => {
    const items = groups.get(group)!;
    if (!items.has(item.key)) items.set(item.key, item);
  };

  for (const source of listed ?? []) {
    const entry = listedItem(source);
    if (entry !== null) add(...entry);
  }

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
    uncitedWebs: listed !== null ? 0 : Math.max(0, (counts?.web ?? 0) - citedWebs.size),
  };
}
