import { expect, it } from "vitest";
import type { SourceMeta } from "../../notes/api";
import { parseNotes } from "../../notes/markdown";
import { NOTES } from "../../notes/testNotes";
import { resourceList } from "../resources";
import {
  citedSources,
  countsText,
  reasonText,
  resourceStates,
  sourceNumber,
  sourceTitle,
  thumbnailOf,
  triageOf,
} from "./state";
import { runBounded } from "./useSourceMetas";

const TOPIC = "subjects/historia/topics/revolucion-industrial";

function meta(file: string, sidecar: Record<string, unknown> | null): [string, SourceMeta] {
  const vaultId = `${TOPIC}/sources/${file}`;
  return [vaultId, { vault_id: vaultId, kind: "notes", media_type: "image/jpeg", size: 1, meta: sidecar, transcription: null }];
}

function states(counts: { notes: number; book: number; pdf: number; web: number }, notes: string | null, metas: Array<[string, SourceMeta]>) {
  const tree = notes === null ? null : parseNotes(notes);
  const items = resourceList(counts, tree).groups.flatMap((g) => g.items);
  return resourceStates("historia", "revolucion-industrial", items, tree, new Map(metas));
}

it("reads the sidecar's triage block, and none when it lacks one", () => {
  expect(triageOf(null)).toBeNull();
  expect(triageOf({ sharpness: 3 })).toBeNull();
  expect(triageOf({ triage: { status: "weird" } })).toBeNull();
  expect(
    triageOf({ triage: { status: "set_aside", reasons: ["duplicate", 3], duplicate_of: "sources/notes/page-001.jpg", decided_by: "auto" } }),
  ).toEqual({ status: "set_aside", reasons: ["duplicate"], duplicateOf: "sources/notes/page-001.jpg", decidedBy: "auto" });
});

it("says each triage reason in Spanish", () => {
  expect(reasonText("blank", null)).toBe("En blanco");
  expect(reasonText("duplicate", "sources/notes/page-003.jpg")).toBe("Repetida de la página 3");
  expect(reasonText("duplicate", null)).toBe("Repetida de otra página");
  expect(reasonText("blurry", null)).toBe("Borrosa");
  expect(reasonText("partial", null)).toBe("Puede estar cortada");
  expect(reasonText("same_content", "sources/notes/page-012.jpg")).toBe("Mismo contenido que la página 12");
});

it("names each source by its number and kind", () => {
  expect(sourceNumber("page-003.jpg")).toBe(3);
  expect(sourceNumber("img-012.png")).toBe(12);
  expect(sourceNumber("002-vapor.md")).toBe(2);
  expect(sourceTitle({ kind: "notes", file: "page-003.jpg" }, "", null)).toBe("Página 3 · apuntes");
  expect(sourceTitle({ kind: "book", file: "page-083.jpg" }, "", null)).toBe("Libro, página 83");
  expect(sourceTitle({ kind: "pdf", file: "page-001.pdf" }, "PDF 1", null)).toBe("PDF");
  expect(sourceTitle({ kind: "pdf", file: "page-001.pdf" }, "PDF 1", { original_name: "tema2.pdf" })).toBe("PDF «tema2.pdf»");
  expect(sourceTitle({ kind: "web", file: "001-vapor.md" }, "Web: 001-vapor.md", null)).toBe("Web: 001-vapor.md");
  expect(sourceTitle({ kind: "web", file: "001-vapor.md" }, "x", { title: "La máquina de vapor" })).toBe("Web: La máquina de vapor");
  expect(sourceTitle({ kind: "images", file: "img-001.png" }, "", null)).toBe("Imagen pegada 1");
});

it("shows a page's flattened image, falling back to the capture", () => {
  const id = `${TOPIC}/sources/notes/page-002.jpg`;
  expect(thumbnailOf(id, { kind: "notes", file: "page-002.jpg" })).toEqual({ src: `${TOPIC}/sources/notes/page-002.page.jpg`, fallback: id });
  expect(thumbnailOf(`${TOPIC}/sources/pdf/page-001.pdf`, { kind: "pdf", file: "page-001.pdf" })?.src).toBe(`${TOPIC}/sources/pdf/page-001.p001.jpg`);
  expect(thumbnailOf(`${TOPIC}/sources/web/001-a.md`, { kind: "web", file: "001-a.md" })).toBeNull();
});

it("finds the sources the notes' footnote definitions link", () => {
  expect([...citedSources(parseNotes(NOTES))].sort()).toEqual([
    "book/page-001.jpg",
    "notes/page-001.jpg",
    "notes/page-002.jpg",
    "pdf/page-001.pdf",
    "web/001-maquina-de-vapor.md",
  ]);
  expect(citedSources(null).size).toBe(0);
});

it("derives pendiente, incorporada and apartada, kept ones first in source order", () => {
  const result = states({ notes: 5, book: 1, pdf: 0, web: 0 }, NOTES, [
    meta("notes/page-001.jpg", { triage: { status: "kept", reasons: [], decided_by: "auto" } }),
    meta("notes/page-003.jpg", { triage: { status: "set_aside", reasons: ["blank"], decided_by: "auto" } }),
    meta("notes/page-004.jpg", { triage: { status: "flagged", reasons: ["partial"], decided_by: "auto" } }),
    meta("notes/page-005.jpg", {
      triage: { status: "set_aside", reasons: ["duplicate"], duplicate_of: "sources/notes/page-002.jpg", decided_by: "student" },
    }),
  ]);
  expect(result.kept.map((e) => [e.title, e.state, e.reasons, e.flagged])).toEqual([
    ["Página 1 · apuntes", "incorporated", [], false],
    ["Página 2 · apuntes", "incorporated", [], false],
    ["Página 4 · apuntes", "pending", ["Puede estar cortada"], true],
    ["Libro, página 1", "incorporated", [], false],
    ["PDF", "incorporated", [], false],
    ["Web: 001-maquina-de-vapor.md", "incorporated", [], false],
  ]);
  expect(result.setAside.map((e) => [e.title, e.reasons, e.byStudent])).toEqual([
    ["Página 3 · apuntes", ["En blanco"], false],
    ["Página 5 · apuntes", ["Repetida de la página 2"], true],
  ]);
  expect(result.counts).toEqual({ pending: 1, incorporated: 5, setAside: 2 });
  expect(result.others.map((i) => i.title)).toEqual(["Transcripción, 00:02:34–00:03:10", "Transcripción, 00:15:00–00:15:42"]);
});

it("keeps legacy sources and unread metadata, and one entry per PDF", () => {
  const result = states({ notes: 2, book: 0, pdf: 1, web: 0 }, null, [meta("notes/page-001.jpg", null)]);
  expect(result.kept.map((e) => [e.title, e.state])).toEqual([
    ["Página 1 · apuntes", "pending"],
    ["Página 2 · apuntes", "pending"],
    ["PDF", "pending"],
  ]);
  // The PDF counted by the summary and cited at page 3 is one source.
  const cited = states({ notes: 0, book: 0, pdf: 1, web: 0 }, NOTES, []);
  expect(cited.kept.filter((e) => e.ref.kind === "pdf")).toHaveLength(1);
});

it("recomputes incorporada from the notes alone", () => {
  const before = states({ notes: 3, book: 0, pdf: 0, web: 0 }, NOTES, []);
  expect(before.kept.find((e) => e.ref.file === "page-003.jpg")?.state).toBe("pending");
  const after = states(
    { notes: 3, book: 0, pdf: 0, web: 0 },
    NOTES.replace("[^ia]: ", "[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)\n[^ia]: ").replace("obrero.[^ia]", "obrero.[^ia][^p3]"),
    [],
  );
  expect(after.kept.find((e) => e.ref.file === "page-003.jpg")?.state).toBe("incorporated");
});

it("writes the counts with singular and plural", () => {
  expect(countsText({ pending: 2, incorporated: 1, setAside: 0 })).toBe("2 pendientes · 1 incorporada · 0 apartadas");
  expect(countsText({ pending: 1, incorporated: 3, setAside: 1 })).toBe("1 pendiente · 3 incorporadas · 1 apartada");
});

it("runs at most `limit` tasks at once", async () => {
  let running = 0;
  let peak = 0;
  const done: number[] = [];
  const tasks = Array.from({ length: 10 }, (_, i) => async () => {
    running++;
    peak = Math.max(peak, running);
    await new Promise((resolve) => setTimeout(resolve, 1));
    running--;
    done.push(i);
  });
  await runBounded(tasks, 3);
  expect(peak).toBe(3);
  expect(done).toHaveLength(10);
  await runBounded([], 3);
});
