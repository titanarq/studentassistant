import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import {
  fetchGeneratedText,
  fetchMaterials,
  fileUrl,
  generatedName,
  generateMaterial,
  previewPath,
  readGenerators,
  readMaterials,
} from "./api";
import { artifact, GENERATED, materialsBody } from "./testMaterials";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("reads the materials status, file names under generated/ and without the manifest", () => {
  const body = materialsBody([
    artifact("esquema", "Esquema", ["esquema.md"]),
    artifact("diapositivas", "Diapositivas", ["diapositivas.md", "diapositivas/figura-01.jpg"], {
      stale: true,
      stale_reason: "Los apuntes han cambiado.",
    }),
    artifact("quiz", "Quiz"),
    { kind: 7 },
  ]);

  const materials = readMaterials(body);

  expect(materials?.hasNotes).toBe(true);
  expect(materials?.artifacts.map((a) => a.kind)).toEqual(["esquema", "diapositivas", "quiz"]);
  expect(materials?.artifacts[0]).toEqual({
    kind: "esquema",
    title: "Esquema",
    generated: true,
    stale: false,
    staleReason: null,
    files: ["esquema.md"],
    builtAt: "2026-09-24T18:30:00Z",
    notesVersion: 3,
    warnings: [],
  });
  expect(materials?.artifacts[1].files).toEqual(["diapositivas.md", "diapositivas/figura-01.jpg"]);
  expect(materials?.artifacts[1].staleReason).toBe("Los apuntes han cambiado.");
  expect(materials?.artifacts[2]).toMatchObject({ generated: false, files: [], builtAt: null, notesVersion: null });
  expect(readMaterials({ artifacts: [] })).toBeNull();
});

it("reads the generators, skipping entries it cannot show", () => {
  expect(readGenerators([{ kind: "quiz", title: "Quiz" }, { title: "sin kind" }])).toEqual([
    { kind: "quiz", title: "Quiz", description: "" },
  ]);
  expect(readGenerators({})).toBeNull();
});

it("names generated files and builds their URLs", () => {
  expect(generatedName("subjects/s/topics/t/generated/diapositivas/figura-01.jpg")).toBe("diapositivas/figura-01.jpg");
  expect(generatedName("generated/quiz.yaml")).toBe("quiz.yaml");
  expect(generatedName("subjects/s/topics/t/notes/apuntes.md")).toBeNull();
  expect(fileUrl("historia", "revolucion-francesa", "diapositivas/figura 1.jpg")).toBe(
    `${GENERATED}/files/diapositivas/figura%201.jpg`,
  );
  expect(previewPath("historia", "revolucion-francesa", "examen.md")).toBe(
    "/subjects/historia/topics/revolucion-francesa/material/examen.md",
  );
});

it("fetches the materials, reporting a 404 with the backend's detail", async () => {
  stubApi({ [GENERATED]: jsonResponse({ detail: "No existe ese tema en la bóveda." }, 404) });

  expect(await fetchMaterials("historia", "revolucion-francesa")).toEqual({
    kind: "not-found",
    detail: "No existe ese tema en la bóveda.",
  });
});

it("fetches a generated file as text", async () => {
  stubApi({
    [`${GENERATED}/files/esquema.md`]: new Response("# Esquema: La Revolución", { status: 200 }),
  });

  expect(await fetchGeneratedText("historia", "revolucion-francesa", "esquema.md")).toEqual({
    kind: "ok",
    value: "# Esquema: La Revolución",
  });
});

it("generates with default options, passing the cost-cap confirmation", async () => {
  const fetchMock = stubApi({
    [`POST ${GENERATED}/esquema`]: jsonResponse({
      subject: "historia",
      topic: "revolucion-francesa",
      kind: "esquema",
      files: [],
      notes: { sha256: "f", version: 3 },
      items: 4,
      warnings: ["Un nodo no cita ninguna sección."],
    }),
  });

  const result = await generateMaterial("historia", "revolucion-francesa", "esquema", true);

  expect(result).toEqual({
    kind: "ok",
    value: { kind: "esquema", notesVersion: 3, warnings: ["Un nodo no cita ninguna sección."] },
  });
  expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({ options: {}, confirm_over_cap: true });
});

it("reports a reached cost cap as a refusal that can be confirmed", async () => {
  stubApi({
    [`POST ${GENERATED}/quiz`]: jsonResponse({ detail: "Se ha alcanzado el límite.", code: "cost_cap_reached" }, 409),
  });

  const result = await generateMaterial("historia", "revolucion-francesa", "quiz");

  expect(result).toMatchObject({ kind: "refused", status: 409, overCap: true, detail: "Se ha alcanzado el límite." });
});
