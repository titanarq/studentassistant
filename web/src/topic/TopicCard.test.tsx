import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { TopicSummary } from "../desk/api";
import TopicCard from "./TopicCard";

function summary(overrides: Partial<TopicSummary> = {}): TopicSummary {
  return {
    subject_id: "historia",
    topic_id: "revolucion-francesa",
    sources: { notes: 6, book: 13, pdf: 1, web: 2 },
    sessions: 2,
    session_minutes: 31.2,
    open_pending: 4,
    notes_version: 3,
    generated: [],
    ...overrides,
  };
}

function field(term: string): HTMLElement {
  const dt = screen.getByText(term, { selector: "dt" });
  return dt.nextElementSibling as HTMLElement;
}

it("shows the card of VISION §2: sources, sessions, pending and material", () => {
  render(<TopicCard summary={summary()} />);

  expect(field("Fuentes")).toHaveTextContent(
    "✓ 6 páginas manuscritas ✓ 13 páginas del libro ✓ 1 PDF ✓ 2 webs",
  );
  expect(field("Sesiones")).toHaveTextContent("✓ 2 (31 min de conversación)");
  expect(field("Pendiente")).toHaveTextContent("4 dudas por revisar");
  expect(field("Material")).toHaveTextContent(
    "✓ Apuntes v3 (versiones) ○ Esquema ○ Quiz ○ Flashcards ○ Examen ○ Diapositivas",
  );
});

it("marks the generated material present from the files under generated/", () => {
  const root = "subjects/historia/topics/revolucion-francesa/generated";
  render(
    <TopicCard
      summary={summary({ generated: [`${root}/outline.md`, `${root}/quiz.yaml`, `${root}/slides/slides.md`] })}
    />,
  );

  expect(field("Material")).toHaveTextContent(
    "✓ Apuntes v3 (versiones) ✓ Esquema ✓ Quiz ○ Flashcards ○ Examen ✓ Diapositivas",
  );
});

it("shows the empty states of a new topic", () => {
  render(
    <TopicCard
      summary={summary({
        sources: { notes: 0, book: 0, pdf: 0, web: 0 },
        sessions: 0,
        session_minutes: 0,
        open_pending: 0,
        notes_version: null,
      })}
    />,
  );

  expect(field("Fuentes")).toHaveTextContent(
    "○ 0 páginas manuscritas ○ 0 páginas del libro ○ 0 PDF ○ 0 webs",
  );
  expect(field("Sesiones")).toHaveTextContent("○ Ninguna todavía");
  expect(field("Pendiente")).toHaveTextContent("Nada por revisar");
  expect(field("Material")).toHaveTextContent("○ Apuntes ○ Esquema");
});

it("uses the singular for one of each", () => {
  render(
    <TopicCard
      summary={summary({ sources: { notes: 1, book: 1, pdf: 0, web: 1 }, sessions: 1, session_minutes: 0.6, open_pending: 1 })}
    />,
  );

  expect(field("Fuentes")).toHaveTextContent("✓ 1 página manuscrita ✓ 1 página del libro ○ 0 PDF ✓ 1 web");
  expect(field("Sesiones")).toHaveTextContent("✓ 1 (1 min de conversación)");
  expect(field("Pendiente")).toHaveTextContent("1 duda por revisar");
});

it("links the notes version to the notes viewer", () => {
  render(<TopicCard summary={summary()} />);

  expect(screen.getByRole("link", { name: "Apuntes v3" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/notes",
  );
  expect(screen.getByRole("link", { name: "versiones" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/versions",
  );
});

it("has no notes link before the first notes version", () => {
  render(<TopicCard summary={summary({ notes_version: null })} />);

  expect(screen.queryByRole("link", { name: /Apuntes/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "versiones" })).not.toBeInTheDocument();
});

it.each([
  [4, "4 dudas por revisar"],
  [0, "Nada por revisar"],
])("links the pending item (%i open) to the pending-doubts panel", (open, text) => {
  render(<TopicCard summary={summary({ open_pending: open })} />);

  expect(screen.getByRole("link", { name: text })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/pending",
  );
});

it("links the quiz item to the quiz page", () => {
  render(<TopicCard summary={summary()} />);

  expect(screen.getByRole("link", { name: "Quiz" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/quiz",
  );
});

it("marks the flashcards present and leaves their downloads to the materials section", () => {
  const root = "subjects/historia/topics/revolucion-francesa/generated";
  render(<TopicCard summary={summary({ generated: [`${root}/flashcards.apkg`, `${root}/flashcards.csv`] })} />);

  expect(field("Material")).toHaveTextContent("✓ Flashcards");
  expect(screen.queryByText("Descargas", { selector: "dt" })).toBeNull();
  expect(screen.queryByRole("link", { name: /Anki/ })).toBeNull();
});

it("links the spaced-repetition practice", () => {
  render(<TopicCard summary={summary()} />);

  expect(screen.getByRole("link", { name: "Practicar con repetición espaciada" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/practice",
  );
});
