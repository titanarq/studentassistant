import { expect, it } from "vitest";
import { parseNotes } from "../notes/markdown";
import { EXCERPT_CHARS, blockExcerpt, whyQuestion } from "./why";

it("quotes a paragraph without its references and names its section", () => {
  const [, paragraph] = parseNotes("## Causas {#causas}\n\nCarbón y [[?hierro]] en abundancia.[^p2]\n").blocks;
  expect(whyQuestion(paragraph, "causas")).toBe("¿Por qué pusiste esto? (en la sección #causas) «Carbón y hierro en abundancia.»");
});

it("flattens lists and tables and cuts long blocks", () => {
  const { blocks } = parseNotes("- Uno.[^p1]\n- Dos.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n");
  expect(blockExcerpt(blocks[0])).toBe("Uno.; Dos.");
  expect(blockExcerpt(blocks[1])).toBe("A | B; 1 | 2");
  const long = blockExcerpt(parseNotes("palabra ".repeat(100)).blocks[0]);
  expect(long.length).toBe(EXCERPT_CHARS);
  expect(long.endsWith("…")).toBe(true);
});

it("has no question for a rule", () => {
  expect(whyQuestion({ type: "rule" }, null)).toBeNull();
});
