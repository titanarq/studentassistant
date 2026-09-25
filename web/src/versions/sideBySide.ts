import type { DiffLine } from "../chat/diff";

/**
 * A unified diff laid out in two columns, the older text on the left and the newer on the right.
 * A context line is on both sides; a run of removed lines is paired, line by line, with the run
 * of added lines next to it (a modified line lands on one row), the shorter run padded with
 * empty cells. Hunk headers become separator rows.
 */

export type SideCell = { kind: "del" | "add" | "context"; text: string } | null;

export type SideRow = { kind: "hunk"; text: string } | { kind: "row"; left: SideCell; right: SideCell };

export function sideBySide(lines: DiffLine[]): SideRow[] {
  const rows: SideRow[] = [];
  let removed: string[] = [];
  let added: string[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(removed.length, added.length); i++) {
      rows.push({
        kind: "row",
        left: i < removed.length ? { kind: "del", text: removed[i] } : null,
        right: i < added.length ? { kind: "add", text: added[i] } : null,
      });
    }
    removed = [];
    added = [];
  };
  for (const line of lines) {
    if (line.kind === "del") {
      removed.push(line.text);
    } else if (line.kind === "add") {
      added.push(line.text);
    } else {
      flush();
      rows.push(
        line.kind === "hunk"
          ? { kind: "hunk", text: line.text }
          : { kind: "row", left: { kind: "context", text: line.text }, right: { kind: "context", text: line.text } },
      );
    }
  }
  flush();
  return rows;
}
