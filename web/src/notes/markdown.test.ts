import { expect, it } from "vitest";
import { footnoteRefs, parseInline, parseNotes } from "./markdown";
import { originalPage, parseProvenance } from "./provenance";
import { NOTES } from "./testNotes";

it("reads headings with their anchors, paragraphs, lists, tables, rules and footnotes", () => {
  const tree = parseNotes(NOTES);

  expect(tree.blocks.map((b) => b.type)).toEqual([
    "heading",
    "paragraph",
    "heading",
    "paragraph",
    "heading",
    "list",
    "table",
    "heading",
    "paragraph",
    "paragraph",
    "rule",
  ]);
  expect(tree.blocks[2]).toEqual({ type: "heading", level: 2, text: "1. Contexto", anchor: "contexto" });
  expect(tree.blocks[0]).toEqual({ type: "heading", level: 1, text: "La Revolución Industrial", anchor: null });
  const list = tree.blocks[5];
  expect(list.type === "list" && list.items.length).toBe(3);
  // The indented paragraph of a loose list stays in its item.
  expect(list.type === "list" && list.items[2].map((b) => b.type)).toEqual(["paragraph", "paragraph"]);
  const table = tree.blocks[6];
  expect(table.type === "table" && table.rows[1]).toEqual(["Algodón", "Industria textil[^b12]"]);
  expect(tree.footnotes.map((f) => f.label)).toEqual(["p1", "p2", "t1", "t2", "b12", "pdf3", "w1", "ia"]);
});

it("nests lists by indentation and keeps fenced code as it is", () => {
  const tree = parseNotes("- uno\n  - uno.a\n  - uno.b\n- dos\n\n```mermaid\ngraph TD\n  A --> B\n```\n");

  const list = tree.blocks[0];
  expect(list.type === "list" && list.items[0].map((b) => b.type)).toEqual(["paragraph", "list"]);
  expect(tree.blocks[1]).toEqual({ type: "code", lang: "mermaid", text: "graph TD\n  A --> B" });
});

it("reads inline emphasis, code, links, footnote references and uncertain words", () => {
  expect(parseInline("**Watt** *mejoró* `x` [enlace](https://e.org) y [[?Newcomen]].[^pdf3]")).toEqual([
    { type: "strong", children: [{ type: "text", text: "Watt" }] },
    { type: "text", text: " " },
    { type: "em", children: [{ type: "text", text: "mejoró" }] },
    { type: "text", text: " " },
    { type: "code", text: "x" },
    { type: "text", text: " " },
    { type: "link", href: "https://e.org", children: [{ type: "text", text: "enlace" }] },
    { type: "text", text: " y " },
    { type: "uncertain", text: "Newcomen" },
    { type: "text", text: "." },
    { type: "footnote", label: "pdf3" },
  ]);
  expect(parseInline("snake_case_word")).toEqual([{ type: "text", text: "snake_case_word" }]);
  expect(footnoteRefs("a[^p1] b[^t1][^p1]")).toEqual(["p1", "t1"]);
});

it("parses every provenance shape of docs/modules/editor.md", () => {
  expect(parseProvenance("p4", "[Apuntes, página 4](../sources/notes/page-004.jpg)")).toEqual({
    kind: "page",
    sourceKind: "notes",
    text: "Apuntes, página 4",
    file: "page-004.jpg",
  });
  expect(parseProvenance("b12", "[Libro, página 12](../sources/book/page-012.jpg)")).toMatchObject({
    kind: "page",
    sourceKind: "book",
  });
  expect(parseProvenance("pdf3", "[PDF, página 3](../sources/pdf/page-001.pdf#page=3)")).toEqual({
    kind: "pdf",
    text: "PDF, página 3",
    file: "page-001.pdf",
    page: 3,
  });
  expect(parseProvenance("w1", "[Web: Vapor](../sources/web/001-maquina-de-vapor.md)")).toEqual({
    kind: "web",
    text: "Web: Vapor",
    file: "001-maquina-de-vapor.md",
  });
  expect(
    parseProvenance(
      "t1",
      "[Transcripción, 00:02:34–00:03:10](../sessions/20260924-183000/transcript.jsonl#t=00:02:34-00:03:10)",
    ),
  ).toEqual({ kind: "transcript", text: "Transcripción, 00:02:34–00:03:10", sessionId: "20260924-183000", span: "00:02:34-00:03:10" });
  expect(parseProvenance("ia", "Ampliado por la IA: no está en tus fuentes").kind).toBe("ia");
  expect(parseProvenance("x", "[Fuera](../../otro/sitio.jpg)")).toEqual({ kind: "unknown", text: "Fuera" });
  expect(parseProvenance("y", "texto suelto")).toEqual({ kind: "unknown", text: "texto suelto" });
});

it("numbers a stored PDF's page as in the original, from the sidecar's first_page", () => {
  expect(originalPage({ first_page: 82 }, 3)).toBe(84);
  expect(originalPage(null, 3)).toBe(3);
  expect(originalPage({ first_page: "82" }, 1)).toBe(1);
});
