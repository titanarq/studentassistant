import { expect, it } from "vitest";
import { diffStats, parseDiff } from "./diff";

const DIFF = `--- apuntes.md (antes)
+++ apuntes.md
@@ -3,3 +3,4 @@
 ## 1. Contexto {#contexto}
-Resumen corto.
+La Revolución Industrial empezó en Gran Bretaña.
+Un ejemplo: la fábrica textil.
 
\\ No newline at end of file
`;

it("keeps hunks, added, removed and context lines and drops the file header", () => {
  const lines = parseDiff(DIFF);
  expect(lines).toEqual([
    { kind: "hunk", text: "@@ -3,3 +3,4 @@" },
    { kind: "context", text: "## 1. Contexto {#contexto}" },
    { kind: "del", text: "Resumen corto." },
    { kind: "add", text: "La Revolución Industrial empezó en Gran Bretaña." },
    { kind: "add", text: "Un ejemplo: la fábrica textil." },
    { kind: "context", text: "" },
  ]);
  expect(diffStats(lines)).toEqual({ added: 2, removed: 1 });
});

it("is empty for an empty diff", () => {
  expect(parseDiff("")).toEqual([]);
});
