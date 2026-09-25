/**
 * The unified diff of `notes/apuntes.md` a chat turn returns (`RevisionResult.diff`, from Python's
 * `difflib.unified_diff`), split into lines to show: hunk headers, added, removed and context
 * lines. The `---`/`+++` file header and `\ No newline at end of file` markers are dropped.
 */

export type DiffLine =
  | { kind: "hunk"; text: string }
  | { kind: "add"; text: string }
  | { kind: "del"; text: string }
  | { kind: "context"; text: string };

export function parseDiff(diff: string): DiffLine[] {
  const lines: DiffLine[] = [];
  let inHunk = false;
  for (const line of diff.split(/\r?\n/)) {
    if (line.startsWith("@@")) {
      inHunk = true;
      lines.push({ kind: "hunk", text: line });
    } else if (!inHunk || line.startsWith("\\")) {
      continue;
    } else if (line.startsWith("+")) {
      lines.push({ kind: "add", text: line.slice(1) });
    } else if (line.startsWith("-")) {
      lines.push({ kind: "del", text: line.slice(1) });
    } else if (line.startsWith(" ")) {
      lines.push({ kind: "context", text: line.slice(1) });
    }
    // Anything else is the empty string after the last newline.
  }
  return lines;
}

/** How many lines the diff adds and removes. */
export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((line) => line.kind === "add").length,
    removed: lines.filter((line) => line.kind === "del").length,
  };
}
