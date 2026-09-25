/**
 * Provenance footnotes of the master notes, as docs/modules/editor.md defines them: each
 * definition is one Markdown link relative to `notes/apuntes.md`, or the AI mark `[^ia]`.
 * `parseProvenance` turns a definition into what the sources panel has to fetch.
 */

import { IA_LABEL } from "./markdown";

export type Provenance =
  /** A handwritten (`notes`) or book page: `../sources/<kind>/page-NNN.<ext>`. */
  | { kind: "page"; sourceKind: "notes" | "book"; text: string; file: string }
  /** A page of a stored PDF: `../sources/pdf/page-NNN.pdf#page=K` (`page` null for the whole). */
  | { kind: "pdf"; text: string; file: string; page: number | null }
  /** A web snapshot: `../sources/web/NNN-<slug>.md`. */
  | { kind: "web"; text: string; file: string }
  /** A transcript span: `../sessions/<id>/transcript.jsonl#t=HH:MM:SS-HH:MM:SS`. */
  | { kind: "transcript"; text: string; sessionId: string; span: string }
  /** `[^ia]`: content the AI added, not in the student's sources. */
  | { kind: "ia"; text: string }
  /** Anything else: shown as it is written. */
  | { kind: "unknown"; text: string };

const LINK = /^\[([^\]]*)\]\(([^)\s]+)\)\s*$/;
const FILE = "([a-z0-9][a-z0-9._-]*)";
const SOURCE = new RegExp(`^\\.\\./sources/(notes|book|pdf|web)/${FILE}(?:#page=(\\d+))?$`);
const TRANSCRIPT = /^\.\.\/sessions\/([A-Za-z0-9][A-Za-z0-9_-]*)\/transcript\.jsonl#t=(\d{2,}:[0-5]\d:[0-5]\d-\d{2,}:[0-5]\d:[0-5]\d)$/;

export function parseProvenance(label: string, definition: string): Provenance {
  if (label === IA_LABEL) return { kind: "ia", text: definition || "Ampliado por la IA: no está en tus fuentes" };
  const link = LINK.exec(definition.trim());
  if (!link) return { kind: "unknown", text: definition };
  const [, text, href] = link;
  const source = SOURCE.exec(href);
  if (source) {
    const [, kind, file, page] = source;
    if (kind === "pdf") return { kind: "pdf", text, file, page: page === undefined ? null : Number(page) };
    if (page !== undefined) return { kind: "unknown", text };
    if (kind === "web") return { kind: "web", text, file };
    return { kind: "page", sourceKind: kind as "notes" | "book", text, file };
  }
  const transcript = TRANSCRIPT.exec(href);
  if (transcript) return { kind: "transcript", text, sessionId: transcript[1], span: transcript[2] };
  return { kind: "unknown", text };
}

/** The vault-relative path of a file under a topic's `sources/<kind>/`, as the read API takes it. */
export function sourceVaultId(subjectId: string, topicId: string, kind: string, file: string): string {
  return `subjects/${subjectId}/topics/${topicId}/sources/${kind}/${file}`;
}

/** `page-004.jpg` -> `page-004` (the stem derived files hang from). */
export function stemOf(file: string): string {
  const dot = file.indexOf(".");
  return dot === -1 ? file : file.slice(0, dot);
}

/**
 * The original PDF's number of page `page` of a stored PDF, from its sidecar -- the rule of
 * `studentassistant.sources.pdf.original_page`: `first_page + page - 1` (`first_page` 1 when
 * the sidecar lacks it).
 */
export function originalPage(meta: Record<string, unknown> | null, page: number): number {
  const first = meta?.first_page;
  return (typeof first === "number" && Number.isInteger(first) ? first : 1) + page - 1;
}
