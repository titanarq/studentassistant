import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import MaterialPreviewPage from "./MaterialPreviewPage";
import { artifact, GENERATED, materialsBody } from "./testMaterials";

afterEach(() => {
  vi.unstubAllGlobals();
});

const TOPICS = jsonResponse({
  subject_id: "historia",
  topics: [{ topic_id: "revolucion-francesa", subject_id: "historia", name: "La Revolución Francesa" }],
});

const OUTLINE = [
  "# Esquema: La Revolución Francesa",
  "",
  "## 1. Causas",
  "",
  "Crisis fiscal y social. <script>alert(1)</script>",
  "",
  "- **1.1 Deuda**: guerras",
  "",
  "## Mapa mental",
  "",
  "```mermaid",
  "mindmap",
  '  root(("La Revolución Francesa"))',
  "```",
].join("\n");

function renderPage(name = "esquema.md") {
  render(<MaterialPreviewPage subjectId="historia" topicId="revolucion-francesa" name={name} />);
}

it("renders a generated Markdown file with the notes renderer, never as markup", async () => {
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${GENERATED}/files/esquema.md`]: new Response(OUTLINE, { status: 200 }),
    [GENERATED]: jsonResponse(materialsBody([artifact("esquema", "Esquema", ["esquema.md"])])),
  });

  renderPage();

  expect(await screen.findByRole("heading", { name: "Esquema: La Revolución Francesa" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 1, name: "Esquema de La Revolución Francesa" })).toBeInTheDocument();
  expect(screen.getByText(/Crisis fiscal y social\. <script>alert\(1\)<\/script>/)).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText(/root\(\("La Revolución Francesa"\)\)/)).toBeInTheDocument();
  expect(await screen.findByText(/De los apuntes v3/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Descargar esquema.md" })).toHaveAttribute(
    "href",
    `${GENERATED}/files/esquema.md`,
  );
  expect(screen.getByRole("link", { name: "← Tema La Revolución Francesa" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa",
  );
  expect(screen.queryByRole("note")).toBeNull();
});

it("names the file of a kind with several and notes when it is stale", async () => {
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${GENERATED}/files/examen-soluciones.md`]: new Response("# Soluciones", { status: 200 }),
    [GENERATED]: jsonResponse(
      materialsBody([
        artifact("examen", "Ejercicios y examen", ["examen.md", "examen-soluciones.md"], {
          stale: true,
          stale_reason: "Los apuntes han cambiado desde que se generó.",
        }),
      ]),
    ),
  });

  renderPage("examen-soluciones.md");

  expect(
    await screen.findByRole("heading", { level: 1, name: "Ejercicios y examen (examen-soluciones) de La Revolución Francesa" }),
  ).toBeInTheDocument();
  expect(await screen.findByRole("note")).toHaveTextContent("Desactualizado Los apuntes han cambiado desde que se generó.");
});

it("shows the backend's detail when the file does not exist", async () => {
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${GENERATED}/files/esquema.md`]: jsonResponse({ detail: "No existe ese archivo en el material generado del tema." }, 404),
    [GENERATED]: jsonResponse(materialsBody([artifact("esquema", "Esquema")])),
  });

  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent("No existe ese archivo en el material generado del tema.");
});
