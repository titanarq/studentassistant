import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App";
import { jsonResponse, stubApi } from "./test/mockApi";

afterEach(() => {
  vi.unstubAllGlobals();
});

// 2026-09-24 12:00 UTC: the same calendar day in every time zone the tests may run in.
const SEP_24 = Date.UTC(2026, 8, 24, 12);

it("lists every subject with its topics, linking each to its topic page", async () => {
  const fetchMock = stubApi({
    "/api/subjects": jsonResponse({
      subjects: [
        { subject_id: "historia", name: "Historia" },
        { subject_id: "fisica", name: "Física" },
      ],
    }),
    "/api/subjects/historia/topics": jsonResponse({
      subject_id: "historia",
      topics: [
        {
          topic_id: "revolucion-francesa",
          subject_id: "historia",
          name: "Tema 4 — La Revolución Francesa",
          last_session_at_ms: SEP_24,
          pending_count: 4,
        },
        { topic_id: "imperio-romano", subject_id: "historia", name: "El Imperio romano", open_session_id: "s-1" },
      ],
    }),
    "/api/subjects/fisica/topics": jsonResponse({ subject_id: "fisica", topics: [] }),
  });

  render(<App />);

  expect(screen.getByRole("heading", { name: "Mesa de estudio" })).toBeInTheDocument();
  const historia = await screen.findByRole("region", { name: "Historia" });
  const link = within(historia).getByRole("link", { name: "Tema 4 — La Revolución Francesa" });
  expect(link).toHaveAttribute("href", "/subjects/historia/topics/revolucion-francesa");
  expect(link.closest("li")).toHaveTextContent("Última sesión: 24 de septiembre de 2026 · 4 dudas por revisar");
  const roman = within(historia).getByRole("link", { name: "El Imperio romano" });
  expect(roman.closest("li")).toHaveTextContent("Sesión abierta");
  expect(roman.closest("li")).not.toHaveTextContent("Última sesión");

  const fisica = screen.getByRole("region", { name: "Física" });
  expect(fisica).toHaveTextContent("Esta asignatura todavía no tiene temas.");
  expect(fetchMock).toHaveBeenCalledWith("/api/subjects");
});

it("shows a topic without the 1.1 fields (older backend) with just its name", async () => {
  stubApi({
    "/api/subjects": jsonResponse({ subjects: [{ subject_id: "historia", name: "Historia" }] }),
    "/api/subjects/historia/topics": jsonResponse({
      subject_id: "historia",
      topics: [{ topic_id: "t1", subject_id: "historia", name: "Tema 1", pending_count: 0 }],
    }),
  });

  render(<App />);

  const link = await screen.findByRole("link", { name: "Tema 1" });
  expect(link.closest("li")).toHaveTextContent(/^Tema 1$/);
});

it("shows the empty desk when there are no subjects yet", async () => {
  stubApi({ "/api/subjects": jsonResponse({ subjects: [] }) });

  render(<App />);

  expect(await screen.findByText(/Todavía no hay asignaturas/)).toBeInTheDocument();
});

it("shows a Spanish error when the backend is unreachable", async () => {
  stubApi({ "/api/subjects": new Error("offline") });

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se pudo cargar la mesa de estudio: No se pudo conectar con el servidor.",
  );
});

it("shows the error state when the subject list is not a protocol body", async () => {
  stubApi({ "/api/subjects": jsonResponse({ subjects: [{ id: "historia" }] }) });

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("El servidor respondió con un error (200).");
});

it("keeps the other subjects when one topic list fails", async () => {
  stubApi({
    "/api/subjects": jsonResponse({
      subjects: [
        { subject_id: "historia", name: "Historia" },
        { subject_id: "fisica", name: "Física" },
      ],
    }),
    "/api/subjects/historia/topics": jsonResponse({ detail: "No se puede abrir la bóveda." }, 503),
    "/api/subjects/fisica/topics": jsonResponse({
      subject_id: "fisica",
      topics: [{ topic_id: "ondas", subject_id: "fisica", name: "Ondas" }],
    }),
  });

  render(<App />);

  expect(await screen.findByRole("link", { name: "Ondas" })).toBeInTheDocument();
  expect(within(screen.getByRole("region", { name: "Historia" })).getByRole("alert")).toHaveTextContent(
    "No se pudieron cargar los temas: El servidor respondió con un error (503).",
  );
});
