import { expect, it } from "vitest";
import { parseDiff } from "../chat/diff";
import { sideBySide } from "./sideBySide";

it("pairs removed with added lines and keeps context on both sides", () => {
  const diff = "--- a\n+++ b\n@@ -1,3 +1,4 @@\n uno\n-dos\n-tres\n+DOS\n cuatro\n+cinco\n";

  expect(sideBySide(parseDiff(diff))).toEqual([
    { kind: "hunk", text: "@@ -1,3 +1,4 @@" },
    { kind: "row", left: { kind: "context", text: "uno" }, right: { kind: "context", text: "uno" } },
    { kind: "row", left: { kind: "del", text: "dos" }, right: { kind: "add", text: "DOS" } },
    { kind: "row", left: { kind: "del", text: "tres" }, right: null },
    { kind: "row", left: { kind: "context", text: "cuatro" }, right: { kind: "context", text: "cuatro" } },
    { kind: "row", left: null, right: { kind: "add", text: "cinco" } },
  ]);
});

it("is empty for an empty diff", () => {
  expect(sideBySide(parseDiff(""))).toEqual([]);
});
