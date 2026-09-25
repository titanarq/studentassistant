import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import MaterialsPanel from "./MaterialsPanel";
import { artifact, GENERATED, GENERATORS, materialsBody } from "./testMaterials";

afterEach(() => {
  vi.unstubAllGlobals();
});

const PAGE = "/subjects/historia/topics/revolucion-francesa";

function renderPanel(onGenerated?: () => void) {
  render(<MaterialsPanel subjectId="historia" topicId="revolucion-francesa" onGenerated={onGenerated} />);
}

function item(name: string) {
  return within(screen.getByRole("listitem", { name }));
}

async function findItem(name: string) {
  return within(await screen.findByRole("listitem", { name }));
}

it("lists every material in the order of study, with its state and description", async () => {
  stubApi({
    "/api/generators": jsonResponse(GENERATORS),
    [GENERATED]: jsonResponse(
      materialsBody([
        artifact("diapositivas", "Diapositivas"),
        artifact("esquema", "Esquema", ["esquema.md"]),
        artifact("quiz", "Quiz", ["quiz.yaml"]),
        artifact("viejo", null, ["viejo.md"]),
      ]),
    ),
  });

  renderPanel();

  await screen.findByRole("listitem", { name: "Esquema" });
  expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
    "Esquema",
    "Quiz",
    "Diapositivas",
    "viejo",
  ]);
  expect(item("Esquema").getByText(/✓ Generado el .* · de los apuntes v3/)).toBeInTheDocument();
  expect(await (await findItem("Esquema")).findByText("Esquema jerárquico del tema con un mapa mental.")).toBeInTheDocument();
  expect(item("Esquema").getByRole("link", { name: "Ver esquema" })).toHaveAttribute("href", `${PAGE}/material/esquema.md`);
  expect(item("Esquema").getByRole("button", { name: "Generar de nuevo" })).toBeEnabled();
  expect(item("Quiz").getByRole("link", { name: "Hacer el quiz" })).toHaveAttribute("href", `${PAGE}/quiz`);
  expect(item("Diapositivas").getByText("○ Sin generar")).toBeInTheDocument();
  expect(item("Diapositivas").getByRole("button", { name: "Generar" })).toBeEnabled();
  // A kind no longer registered is shown, but cannot be generated.
  expect(item("viejo").queryByRole("button")).toBeNull();
  expect(screen.queryByText("Desactualizado")).toBeNull();
});

it("marks a stale material with the backend's reason", async () => {
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(
      materialsBody([
        artifact("esquema", "Esquema", ["esquema.md"], {
          stale: true,
          stale_reason: "Los apuntes han cambiado desde que se generó (apuntes v3).",
        }),
      ]),
    ),
  });

  renderPanel();

  expect(await (await findItem("Esquema")).findByText("Desactualizado")).toBeInTheDocument();
  expect(item("Esquema").getByText("Los apuntes han cambiado desde que se generó (apuntes v3).")).toBeInTheDocument();
});

it("links the Anki deck, CSV, PDF and PowerPoint files for download", async () => {
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(
      materialsBody([
        artifact("flashcards", "Flashcards", ["flashcards.apkg", "flashcards.csv", "flashcards.yaml"]),
        artifact("diapositivas", "Diapositivas", [
          "diapositivas.md",
          "diapositivas.pdf",
          "diapositivas.pptx",
          "diapositivas/figura-01.jpg",
        ]),
      ]),
    ),
  });

  renderPanel();

  const files = `${GENERATED}/files`;
  const anki = await (await findItem("Flashcards")).findByRole("link", { name: "flashcards (Anki)" });
  expect(anki).toHaveAttribute("href", `${files}/flashcards.apkg`);
  expect(anki).toHaveAttribute("download");
  expect(item("Flashcards").getByRole("link", { name: "flashcards (CSV)" })).toHaveAttribute("href", `${files}/flashcards.csv`);
  expect(item("Flashcards").queryByRole("link", { name: /yaml/ })).toBeNull();
  expect(item("Diapositivas").getByRole("link", { name: "diapositivas (PDF)" })).toHaveAttribute(
    "href",
    `${files}/diapositivas.pdf`,
  );
  expect(item("Diapositivas").getByRole("link", { name: "diapositivas (PowerPoint)" })).toHaveAttribute("download");
  expect(item("Diapositivas").getAllByRole("link").map((l) => l.textContent)).toEqual([
    "Ver diapositivas",
    "diapositivas (PDF)",
    "diapositivas (PowerPoint)",
  ]);
});

it("generates a material, shows its warnings and reads the section again", async () => {
  let reads = 0;
  const onGenerated = vi.fn();
  const fetchMock = stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: () => {
      reads++;
      return jsonResponse(
        materialsBody([artifact("esquema", "Esquema", reads === 1 ? [] : ["esquema.md"])]),
      );
    },
    [`POST ${GENERATED}/esquema`]: jsonResponse({
      kind: "esquema",
      notes: { sha256: "f", version: 3 },
      warnings: ["Un nodo no cita ninguna sección de los apuntes."],
    }),
  });

  render(<MaterialsPanel subjectId="historia" topicId="revolucion-francesa" />);
  fireEvent.click(await (await findItem("Esquema")).findByRole("button", { name: "Generar" }));

  expect(await screen.findByText("Un nodo no cita ninguna sección de los apuntes.")).toBeInTheDocument();
  expect(await (await findItem("Esquema")).findByRole("button", { name: "Generar de nuevo" })).toBeEnabled();
  expect(item("Esquema").getByRole("link", { name: "Ver esquema" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(`${GENERATED}/esquema`, expect.objectContaining({ method: "POST" }));
  expect(onGenerated).not.toHaveBeenCalled();
});

it("hands the reload to the page when it asks for it", async () => {
  const onGenerated = vi.fn();
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(materialsBody([artifact("esquema", "Esquema")])),
    [`POST ${GENERATED}/esquema`]: jsonResponse({ kind: "esquema", notes: { sha256: "f" }, warnings: [] }),
  });

  renderPanel(onGenerated);
  fireEvent.click(await (await findItem("Esquema")).findByRole("button", { name: "Generar" }));

  await waitFor(() => expect(onGenerated).toHaveBeenCalledTimes(1));
});

it("offers to generate anyway past the cost cap", async () => {
  const bodies: unknown[] = [];
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(materialsBody([artifact("examen", "Ejercicios y examen")])),
    [`POST ${GENERATED}/examen`]: () =>
      bodies.length === 1
        ? jsonResponse({ detail: "Se ha alcanzado el límite de gasto del tema.", code: "cost_cap_reached" }, 409)
        : jsonResponse({ kind: "examen", notes: { sha256: "f" }, warnings: [] }),
  });
  const realFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: string, init?: RequestInit) => {
    if (init?.method === "POST") bodies.push(JSON.parse(init.body as string));
    return realFetch(input, init);
  });

  renderPanel();
  fireEvent.click(await (await findItem("Ejercicios y examen")).findByRole("button", { name: "Generar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Se ha alcanzado el límite de gasto del tema.");
  fireEvent.click(screen.getByRole("button", { name: "Generar igualmente" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(bodies).toEqual([
    { options: {}, confirm_over_cap: false },
    { options: {}, confirm_over_cap: true },
  ]);
});

it("shows a refusal as the backend's Spanish detail, with no confirmation", async () => {
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(materialsBody([artifact("esquema", "Esquema")])),
    [`POST ${GENERATED}/esquema`]: jsonResponse(
      { detail: "La generación de material no está disponible: el servidor no usa Claude." },
      503,
    ),
  });

  renderPanel();
  fireEvent.click(await (await findItem("Esquema")).findByRole("button", { name: "Generar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("el servidor no usa Claude");
  expect(screen.queryByRole("button", { name: "Generar igualmente" })).toBeNull();
});

it("cannot generate before the topic has notes", async () => {
  stubApi({
    "/api/generators": jsonResponse([]),
    [GENERATED]: jsonResponse(materialsBody([artifact("esquema", "Esquema")], false)),
  });

  renderPanel();

  expect(await (await findItem("Esquema")).findByRole("button", { name: "Generar" })).toBeDisabled();
  expect(screen.getByText(/Todavía no hay apuntes/)).toBeInTheDocument();
});

it("reports a materials list that cannot be read, as plain text", async () => {
  stubApi({ "/api/generators": new Error("down"), [GENERATED]: new Error("down") });

  renderPanel();

  expect(await screen.findByText(/No se pudo cargar el material del tema: No se pudo conectar/)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});
