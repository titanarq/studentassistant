/**
 * Source preservation for the visual editor (#316): Milkdown (remark) re-serialises every block
 * in its own style (`*` bullets, padded tables, escaped `_` and `[`), so a load and a save with no
 * edit would rewrite the whole document. Here the document keeps, for every top-level block, the
 * exact text it was loaded from; on save a block that is still equal to the loaded one is written
 * back as that text and only the blocks the student changed (or added) are serialised. So an
 * untouched document comes back byte for byte and a one-word edit changes only its block.
 */

import { Mark, type Node as ProseNode } from "@milkdown/prose/model";

/** The loaded document: its top-level blocks, their source text and what lay between them. */
export interface SourceMap {
  nodes: ProseNode[];
  /** `chunks[i]` is the source of `nodes[i]`. */
  chunks: string[];
  /** `separators[i]` lay between `chunks[i]` and `chunks[i + 1]`. */
  separators: string[];
  /** The text before the first block and after the last one. */
  lead: string;
  trail: string;
}

export interface Positioned {
  position?: { start: { offset?: number }; end: { offset?: number } };
}

/**
 * The source map of `markdown` from the top-level mdast children (which carry offsets) and the
 * top-level nodes they became, or `null` when the two do not line up one to one (then the whole
 * document is serialised on save, which is only a style change).
 */
export function buildSourceMap(markdown: string, children: Positioned[], nodes: ProseNode[]): SourceMap | null {
  if (children.length !== nodes.length) return null;
  const ranges: Array<[number, number]> = [];
  for (const child of children) {
    const start = child.position?.start.offset;
    const end = child.position?.end.offset;
    if (start === undefined || end === undefined) return null;
    if (ranges.length > 0 && start < ranges[ranges.length - 1][1]) return null;
    ranges.push([start, end]);
  }
  if (ranges.length === 0) return { nodes, chunks: [], separators: [], lead: "", trail: markdown };
  return {
    nodes,
    chunks: ranges.map(([start, end]) => markdown.slice(start, end)),
    separators: ranges.slice(1).map(([start], i) => markdown.slice(ranges[i][1], start)),
    lead: markdown.slice(0, ranges[0][0]),
    trail: markdown.slice(ranges[ranges.length - 1][1]),
  };
}

/**
 * Whether two blocks are the same content. The heading `id` attribute is derived by Milkdown from
 * the text (and resynced after load), so it is not compared.
 */
export function sameBlock(a: ProseNode, b: ProseNode): boolean {
  if (a === b || a.eq(b)) return true;
  if (a.type !== b.type || !sameAttrs(a, b) || !Mark.sameSet(a.marks, b.marks)) return false;
  if (a.isText) return a.text === b.text;
  if (a.childCount !== b.childCount) return false;
  for (let i = 0; i < a.childCount; i++) if (!sameBlock(a.child(i), b.child(i))) return false;
  return true;
}

function sameAttrs(a: ProseNode, b: ProseNode): boolean {
  const keys = new Set([...Object.keys(a.attrs), ...Object.keys(b.attrs)]);
  for (const key of keys) {
    if (key !== "id" && JSON.stringify(a.attrs[key]) !== JSON.stringify(b.attrs[key])) return false;
  }
  return true;
}

/** For every node of `current`, the index of the loaded node it is (a longest common subsequence). */
export function alignBlocks(loaded: ProseNode[], current: ProseNode[]): Array<number | null> {
  const n = loaded.length;
  const m = current.length;
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i][j] = sameBlock(loaded[i], current[j])
        ? table[i + 1][j + 1] + 1
        : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const match: Array<number | null> = new Array<number | null>(m).fill(null);
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (sameBlock(loaded[i], current[j])) {
      match[j] = i;
      i++;
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) i++;
    else j++;
  }
  return match;
}

const FOOTNOTE_DEFINITION = "footnote_definition";

/**
 * The Markdown of `current`: loaded blocks as their source, the others through `serialise`
 * (one block's Markdown, without the trailing newline).
 */
export function assemble(map: SourceMap | null, current: ProseNode[], serialise: (node: ProseNode) => string): string {
  if (map === null) {
    return current.length === 0 ? "" : `${joinFresh(current, current.map(serialise))}\n`;
  }
  const match = alignBlocks(map.nodes, current);
  let out = "";
  current.forEach((node, index) => {
    const from = match[index];
    const text = from === null ? serialise(node) : map.chunks[from];
    if (index === 0) out += from === 0 ? map.lead : "";
    else {
      const before = match[index - 1];
      if (from !== null && before !== null && from === before + 1) out += map.separators[before];
      else out += separator(current[index - 1], node);
    }
    out += text;
  });
  if (current.length === 0) return "";
  const last = match[current.length - 1];
  return out + (last === map.nodes.length - 1 ? map.trail : "\n");
}

/** A blank line between blocks, a single newline inside a run of footnote definitions. */
function separator(before: ProseNode, after: ProseNode): string {
  return before.type.name === FOOTNOTE_DEFINITION && after.type.name === FOOTNOTE_DEFINITION ? "\n" : "\n\n";
}

function joinFresh(nodes: ProseNode[], texts: string[]): string {
  return texts.reduce((out, text, i) => (i === 0 ? text : out + separator(nodes[i - 1], nodes[i]) + text), "");
}
