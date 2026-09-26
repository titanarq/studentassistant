/**
 * A small Markdown reader for the master notes (`notes/apuntes.md`, ADR-0005 and
 * docs/modules/editor.md): headings with stable anchors (`## 2. Causas {#causas}`), paragraphs,
 * bullet and numbered lists (nested by indentation, with paragraphs inside items), pipe tables,
 * `---` rules, fenced code (schemes transcribed as mermaid), block quotes and footnote
 * definitions (`[^p4]: [Apuntes, página 4](../sources/notes/page-004.jpg)`).
 *
 * It builds a plain tree the viewer renders as React elements, so no HTML of the notes ever
 * reaches the DOM as markup. Anything it does not recognise is kept as a paragraph, as the
 * backend's parser does.
 */

export type Block =
  | { type: "heading"; level: number; text: string; anchor: string | null }
  | { type: "paragraph"; text: string }
  | { type: "list"; ordered: boolean; start: number; items: Block[][] }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "rule" }
  | { type: "code"; lang: string; text: string }
  | { type: "quote"; blocks: Block[] };

export interface FootnoteDefinition {
  label: string;
  text: string;
}

export interface NotesTree {
  blocks: Block[];
  /** Every footnote definition in document order; a repeated label keeps the first. */
  footnotes: FootnoteDefinition[];
}

export type Inline =
  | { type: "text"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "em"; children: Inline[] }
  | { type: "code"; text: string }
  | { type: "link"; href: string; children: Inline[] }
  /** `![Imagen pegada 1](../sources/images/img-001.png)`: a pasted image (#316). */
  | { type: "image"; src: string; alt: string }
  | { type: "footnote"; label: string }
  /** `[[?word]]`: a word the page transcription was unsure of. */
  | { type: "uncertain"; text: string };

const HEADING = /^(#{1,6})\s+(.*?)\s*$/;
const ANCHOR = /\s*\{#([A-Za-z0-9_-]+)\}\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const FENCE = /^\s{0,3}(```+|~~~+)\s*([^`\s]*)/;
const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const EMPTY_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s*$/;
const FOOTNOTE_DEF = /^\[\^([^\]\s]+)\]:\s?(.*)$/;
const TABLE_DELIMITER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;

const isBlank = (line: string) => line.trim() === "";
const indentOf = (line: string) => line.length - line.trimStart().length;

function splitRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  const cells: string[] = [];
  let cell = "";
  for (let i = 0; i < row.length; i++) {
    if (row[i] === "\\" && row[i + 1] === "|") {
      cell += "|";
      i++;
    } else if (row[i] === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += row[i];
    }
  }
  cells.push(cell.trim());
  return cells;
}

function listMarker(line: string): { indent: number; ordered: boolean; start: number; rest: string } | null {
  const match = LIST_ITEM.exec(line) ?? EMPTY_ITEM.exec(line);
  if (!match) return null;
  const marker = match[2];
  const ordered = /\d/.test(marker);
  return { indent: match[1].length, ordered, start: ordered ? Number.parseInt(marker, 10) : 1, rest: match[3] ?? "" };
}

/** Whether `line` begins a block other than a paragraph continuation. */
function startsBlock(line: string): boolean {
  return (
    HEADING.test(line) ||
    RULE.test(line) ||
    FENCE.test(line) ||
    QUOTE.test(line) ||
    FOOTNOTE_DEF.test(line) ||
    listMarker(line) !== null
  );
}

function parseBlocks(lines: string[], footnotes: FootnoteDefinition[]): Block[] {
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      i++;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const closing = fence[1][0];
      const body: string[] = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s{0,3}${closing === "`" ? "`" : "~"}{${fence[1].length},}\\s*$`).test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++; // the closing fence (or the end of the text)
      blocks.push({ type: "code", lang: fence[2], text: body.join("\n") });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const anchor = ANCHOR.exec(heading[2]);
      blocks.push({
        type: "heading",
        level: heading[1].length,
        text: anchor ? heading[2].slice(0, anchor.index) : heading[2],
        anchor: anchor ? anchor[1] : null,
      });
      i++;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ type: "rule" });
      i++;
      continue;
    }

    const definition = FOOTNOTE_DEF.exec(line);
    if (definition) {
      let text = definition[2];
      i++;
      // Indented continuation lines belong to the definition.
      while (i < lines.length && !isBlank(lines[i]) && indentOf(lines[i]) >= 2) {
        text += ` ${lines[i].trim()}`;
        i++;
      }
      if (!footnotes.some((f) => f.label === definition[1])) {
        footnotes.push({ label: definition[1], text: text.trim() });
      }
      continue;
    }

    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push((QUOTE.exec(lines[i]) as RegExpExecArray)[1]);
        i++;
      }
      blocks.push({ type: "quote", blocks: parseBlocks(body, footnotes) });
      continue;
    }

    const marker = listMarker(line);
    if (marker) {
      const [list, next] = parseList(lines, i, marker.indent, marker.ordered, marker.start, footnotes);
      blocks.push(list);
      i = next;
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && TABLE_DELIMITER.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && !isBlank(lines[i]) && lines[i].includes("|")) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    const paragraph = [line.trim()];
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) {
      paragraph.push(lines[i].trim());
      i++;
    }
    blocks.push({ type: "paragraph", text: paragraph.join("\n") });
  }
  return blocks;
}

/**
 * A list starting at `lines[start]`, its markers at `indent`: each item's own lines (the marker
 * line, deeper-indented lines and lazy continuations) are dedented and parsed as blocks, so
 * nested lists and paragraphs inside an item come out as its children. A blank line followed by
 * another marker at `indent` or by indented content keeps the list going (a loose list).
 */
function parseList(
  lines: string[],
  start: number,
  indent: number,
  ordered: boolean,
  first: number,
  footnotes: FootnoteDefinition[],
): [Block, number] {
  const items: Block[][] = [];
  let i = start;
  while (i < lines.length) {
    const marker = listMarker(lines[i]);
    if (!marker || marker.indent !== indent || marker.ordered !== ordered) break;
    const contentIndent = indent + 2;
    const body = [marker.rest];
    i++;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) {
        // Look past blank lines: indented content continues this item.
        let j = i;
        while (j < lines.length && isBlank(lines[j])) j++;
        if (j < lines.length && indentOf(lines[j]) >= contentIndent) {
          for (; i < j; i++) body.push("");
          continue;
        }
        break;
      }
      const inner = listMarker(line);
      if (inner && inner.indent <= indent) break;
      if (indentOf(line) >= contentIndent || inner) {
        body.push(line.slice(Math.min(indentOf(line), contentIndent)));
      } else if (startsBlock(line)) {
        break;
      } else {
        body.push(line.trim()); // a lazy continuation of the item's paragraph
      }
      i++;
    }
    items.push(parseBlocks(body, footnotes));
    // A loose list: blank lines then another item at the same indent.
    let j = i;
    while (j < lines.length && isBlank(lines[j])) j++;
    const again = j < lines.length ? listMarker(lines[j]) : null;
    if (again && again.indent === indent && again.ordered === ordered) i = j;
    else break;
  }
  return [{ type: "list", ordered, start: first, items }, i];
}

export function parseNotes(text: string): NotesTree {
  const footnotes: FootnoteDefinition[] = [];
  const blocks = parseBlocks(text.replace(/\r\n?/g, "\n").split("\n"), footnotes);
  return { blocks, footnotes };
}

// -- inline --------------------------------------------------------------------------------------

function findClosing(text: string, from: number, delimiter: string): number {
  let index = text.indexOf(delimiter, from);
  while (index !== -1 && text[index - 1] === "\\") index = text.indexOf(delimiter, index + 1);
  return index;
}

/** The inline content of a paragraph, cell or heading. */
export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let buffer = "";
  const flush = () => {
    if (buffer !== "") out.push({ type: "text", text: buffer });
    buffer = "";
  };
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    const char = text[i];

    if (char === "\\" && i + 1 < text.length && /[\\`*_[\]()#|!{}.+-]/.test(text[i + 1])) {
      buffer += text[i + 1];
      i += 2;
      continue;
    }

    const footnote = /^\[\^([^\]\s]+)\]/.exec(rest);
    if (footnote) {
      flush();
      out.push({ type: "footnote", label: footnote[1] });
      i += footnote[0].length;
      continue;
    }

    const uncertain = /^\[\[\?([^\]]+)\]\]/.exec(rest);
    if (uncertain) {
      flush();
      out.push({ type: "uncertain", text: uncertain[1] });
      i += uncertain[0].length;
      continue;
    }

    if (char === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ type: "code", text: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    if (char === "!" && text[i + 1] === "[") {
      const image = /^!\[([^\]]*)\]\(([^)\s]*)\)/.exec(rest);
      if (image) {
        flush();
        out.push({ type: "image", alt: image[1], src: image[2] });
        i += image[0].length;
        continue;
      }
    }

    if (char === "[") {
      const link = /^\[([^\]]*)\]\(([^)\s]*)\)/.exec(rest);
      if (link) {
        flush();
        out.push({ type: "link", href: link[2], children: parseInline(link[1]) });
        i += link[0].length;
        continue;
      }
    }

    if ((char === "*" || char === "_") && text[i + 1] === char) {
      const delimiter = char + char;
      const end = findClosing(text, i + 2, delimiter);
      if (end > i + 2) {
        flush();
        out.push({ type: "strong", children: parseInline(text.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }

    if (char === "*" || (char === "_" && !/\w/.test(text[i - 1] ?? ""))) {
      const end = findClosing(text, i + 1, char);
      if (end > i + 1 && text[i + 1] !== " " && (char === "*" || !/\w/.test(text[end + 1] ?? ""))) {
        flush();
        out.push({ type: "em", children: parseInline(text.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }

    buffer += char;
    i++;
  }
  flush();
  return out;
}

/** Every footnote label a text cites, in order, each once. */
export function footnoteRefs(text: string): string[] {
  const labels: string[] = [];
  for (const match of text.matchAll(/\[\^([^\]\s]+)\]/g)) {
    if (!labels.includes(match[1])) labels.push(match[1]);
  }
  return labels;
}

/** The label of the AI footnote of ADR-0005. */
export const IA_LABEL = "ia";
