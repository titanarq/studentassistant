import { expect, it } from "vitest";
import { chipTitle, pruned, ranged, toggled, topicSourceId, type SelectedSource } from "./selection";

const page = (n: number): SelectedSource => ({ id: `sources/notes/page-00${n}.jpg`, title: `Pág. ${n}` });
const ids = (list: readonly SelectedSource[]) => list.map((s) => s.id.split("/").at(-1));
const VISIBLE = [1, 2, 3, 4, 5].map(page);

it("names a source by its topic-relative id and a short chip title", () => {
  expect(topicSourceId({ kind: "notes", file: "page-003.jpg" })).toBe("sources/notes/page-003.jpg");
  expect(chipTitle({ kind: "notes", file: "page-003.jpg" }, "Página 3 · apuntes")).toBe("Pág. 3");
  expect(chipTitle({ kind: "book", file: "page-012.jpg" }, "Libro, página 12")).toBe("Libro p. 12");
  expect(chipTitle({ kind: "pdf", file: "001-tema2.pdf" }, "PDF «tema2.pdf»")).toBe("tema2.pdf");
  expect(chipTitle({ kind: "pdf", file: "001-tema2.pdf" }, "PDF")).toBe("PDF");
  expect(chipTitle({ kind: "web", file: "002-wiki.md" }, "Web: La máquina de vapor")).toBe("La máquina de vapor");
  expect(chipTitle({ kind: "images", file: "img-001.png" }, "Imagen pegada 1")).toBe("Imagen 1");
});

it("toggles a source in and out, keeping the selection order", () => {
  const one = toggled([], page(3));
  const two = toggled(one, page(1));
  expect(ids(two)).toEqual(["page-003.jpg", "page-001.jpg"]);
  expect(ids(toggled(two, page(3)))).toEqual(["page-001.jpg"]);
});

it("selects the range from the anchor in the visible order, either direction", () => {
  expect(ids(ranged([page(2)], VISIBLE, page(2).id, page(4)))).toEqual(["page-002.jpg", "page-003.jpg", "page-004.jpg"]);
  expect(ids(ranged([page(4)], VISIBLE, page(4).id, page(2)))).toEqual(["page-004.jpg", "page-002.jpg", "page-003.jpg"]);
  // Shift-clicking a selected card deselects the range.
  expect(ids(ranged(VISIBLE, VISIBLE, page(1).id, page(3)))).toEqual(["page-004.jpg", "page-005.jpg"]);
  // No anchor (or one no longer visible): a plain toggle.
  expect(ids(ranged([], VISIBLE, null, page(4)))).toEqual(["page-004.jpg"]);
  expect(ids(ranged([], VISIBLE, "sources/notes/page-009.jpg", page(4)))).toEqual(["page-004.jpg"]);
});

it("prunes ids no longer listed and refreshes titles, keeping the same array when nothing changes", () => {
  const selected = [page(1), page(2)];
  expect(pruned(selected, new Map([[page(1).id, "Pág. 1"], [page(2).id, "Pág. 2"], [page(3).id, "Pág. 3"]]))).toBe(selected);
  expect(pruned(selected, new Map([[page(2).id, "Pág. 2"]]))).toEqual([page(2)]);
  expect(pruned(selected, new Map([[page(1).id, "Uno"], [page(2).id, "Pág. 2"]]))).toEqual([{ id: page(1).id, title: "Uno" }, page(2)]);
});
