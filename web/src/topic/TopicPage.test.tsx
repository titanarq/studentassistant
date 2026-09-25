import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import TopicPage from "./TopicPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

const SUBJECTS = jsonResponse({ subjects: [{ subject_id: "historia", name: "Historia" }] });
const TOPICS = jsonResponse({
  subject_id: "historia",
  topics: [{ topic_id: "revolucion-francesa", subject_id: "historia", name: "La Revolución Francesa" }],
});
const SUMMARY_PATH = "/api/subjects/historia/topics/revolucion-francesa/summary";

function summary(pdf: number) {
  return jsonResponse({
    subject_id: "historia",
    topic_id: "revolucion-francesa",
    sources: { notes: 6, book: 0, pdf, web: 0 },
    sessions: 2,
    session_minutes: 31,
    open_pending: 4,
    notes_version: 3,
    generated: [],
  });
}

function renderPage() {
  render(<TopicPage subjectId="historia" topicId="revolucion-francesa" />);
}

it("shows the subject and topic names, the topic card and the PDF upload", async () => {
  stubApi({ "/api/subjects": SUBJECTS, "/api/subjects/historia/topics": TOPICS, [SUMMARY_PATH]: summary(0) });

  renderPage();

  expect(await screen.findByRole("heading", { name: "Tema La Revolución Francesa" })).toBeInTheDocument();
  expect(await screen.findByText("Asignatura Historia")).toBeInTheDocument();
  expect(await screen.findByRole("region", { name: "Resumen del tema" })).toHaveTextContent(
    "✓ 2 (31 min de conversación)",
  );
  expect(screen.getByRole("form", { name: "Añadir un PDF" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "← Mesa de estudio" })).toHaveAttribute("href", "/");
});

it("reloads the card after a PDF is added", async () => {
  let summaries = 0;
  stubApi({
    "/api/subjects": SUBJECTS,
    "/api/subjects/historia/topics": TOPICS,
    [SUMMARY_PATH]: () => summary(summaries++ === 0 ? 0 : 1),
    "POST /api/subjects/historia/topics/revolucion-francesa/sources/pdf": jsonResponse(
      {
        source_id: "sources/pdf/p.pdf",
        vault_id: "subjects/historia/topics/revolucion-francesa/sources/pdf/p.pdf",
        original_name: "Tema 4.pdf",
        original_page_count: 10,
        first_page: 1,
        last_page: 10,
        page_count: 10,
        pages_without_text: [],
      },
      201,
    ),
  });

  renderPage();
  expect(await screen.findByText(/○ 0 PDF/)).toBeInTheDocument();

  const file = new File(["%PDF-1.7"], "Tema 4.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Archivo PDF"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  expect(await screen.findByText(/✓ 1 PDF/)).toBeInTheDocument();
});

it("shows the backend's Spanish detail for an unknown topic, without the upload", async () => {
  stubApi({
    "/api/subjects": SUBJECTS,
    "/api/subjects/historia/topics": TOPICS,
    [SUMMARY_PATH]: jsonResponse({ detail: "No existe ese tema en la bóveda." }, 404),
  });

  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se pudo cargar el resumen del tema: No existe ese tema en la bóveda.",
  );
  expect(screen.queryByRole("form", { name: "Añadir un PDF" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Tema La Revolución Francesa" })).toBeInTheDocument();
});

it("keeps the ids as names and shows an error when the backend is down", async () => {
  stubApi({ "/api/subjects": new Error("x"), "/api/subjects/historia/topics": new Error("x"), [SUMMARY_PATH]: new Error("x") });

  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo conectar con el servidor.");
  expect(screen.getByRole("heading", { name: "Tema revolucion-francesa" })).toBeInTheDocument();
  expect(screen.getByText("Asignatura historia")).toBeInTheDocument();
});
