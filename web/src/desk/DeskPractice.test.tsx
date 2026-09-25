import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import DeskPractice from "./DeskPractice";
import { countsText, formatDue } from "./practiceSummary";

afterEach(() => {
  vi.unstubAllGlobals();
});

function topic(overrides: Record<string, unknown> = {}) {
  return {
    subject_id: "historia",
    subject_name: "Historia",
    topic_id: "revolucion-francesa",
    topic_title: "La Revolución Francesa",
    due: 12,
    new: 5,
    next_due: null,
    ...overrides,
  };
}

function summary(topics: Record<string, unknown>[], warnings: string[] = []) {
  const totals = topics.reduce(
    (acc: { due: number; new: number; topics: number }, t) => ({
      due: acc.due + (t.due as number),
      new: acc.new + (t.new as number),
      topics: acc.topics + 1,
    }),
    { due: 0, new: 0, topics: 0 },
  );
  return jsonResponse({ now: "2026-09-25T08:00:00Z", topics, totals, warnings });
}

it("lists the totals and one linked row per topic with due or new items", async () => {
  stubApi({
    "/api/practice/summary": summary([
      topic(),
      topic({ subject_id: "mates", subject_name: "Matemáticas", topic_id: "derivadas", topic_title: "Derivadas", due: 1, new: 0 }),
      topic({ subject_id: "mates", subject_name: "Matemáticas", topic_id: "limites", topic_title: "Límites", due: 0, new: 0, next_due: "2026-09-27T10:00:00Z" }),
    ]),
  });

  render(<DeskPractice />);

  const block = await screen.findByRole("region", { name: "Repasos para hoy" });
  expect(within(block).getByText(/en 2 temas/)).toHaveTextContent("13 pendientes, 5 nuevas en 2 temas.");
  const rows = within(block).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("Historia · La Revolución Francesa — 12 pendientes, 5 nuevas");
  expect(rows[1]).toHaveTextContent("Matemáticas · Derivadas — 1 pendiente");
  expect(within(rows[0]).getByRole("link")).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/practice",
  );
  expect(within(rows[1]).getByRole("link", { name: "Matemáticas · Derivadas" })).toHaveAttribute(
    "href",
    "/subjects/mates/topics/derivadas/practice",
  );
  expect(within(block).queryByText(/Límites/)).not.toBeInTheDocument();
});

it("says there is nothing to review today with the earliest next due date", async () => {
  stubApi({
    "/api/practice/summary": summary([
      topic({ due: 0, new: 0, next_due: "2026-09-28T10:00:00Z" }),
      topic({ topic_id: "imperio-romano", due: 0, new: 0, next_due: "2026-09-26T09:30:00Z" }),
    ]),
  });

  render(<DeskPractice />);

  const block = await screen.findByRole("region", { name: "Repasos para hoy" });
  expect(within(block).getByText(/Nada que repasar hoy/)).toHaveTextContent(
    `Nada que repasar hoy. Próximo repaso: ${formatDue("2026-09-26T09:30:00Z")}.`,
  );
  expect(within(block).queryByRole("link")).not.toBeInTheDocument();
});

it("hides the block when there is no practice material at all", async () => {
  const fetchMock = stubApi({ "/api/practice/summary": summary([]) });

  const { container } = render(<DeskPractice />);

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/practice/summary"));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByRole("region", { name: "Repasos para hoy" })).not.toBeInTheDocument();
  expect(container).toBeEmptyDOMElement();
});

it("lists the warnings of the response", async () => {
  stubApi({
    "/api/practice/summary": summary([topic()], ["No se pudo leer el tema «roto» de Historia."]),
  });

  render(<DeskPractice />);

  const warnings = await screen.findByRole("list", { name: "Avisos de los repasos" });
  expect(warnings).toHaveTextContent("No se pudo leer el tema «roto» de Historia.");
});

it("reports a failed read in one discreet Spanish line", async () => {
  stubApi({ "/api/practice/summary": jsonResponse({ detail: "x" }, 503) });

  render(<DeskPractice />);

  expect(
    await screen.findByText("No se pudieron cargar los repasos: El servidor respondió con un error (503)."),
  ).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reports an unreachable server", async () => {
  stubApi({ "/api/practice/summary": new Error("offline") });

  render(<DeskPractice />);

  expect(
    await screen.findByText("No se pudieron cargar los repasos: No se pudo conectar con el servidor."),
  ).toBeInTheDocument();
});

it("puts counts in Spanish, leaving zeros out", () => {
  expect(countsText(12, 5)).toBe("12 pendientes, 5 nuevas");
  expect(countsText(1, 1)).toBe("1 pendiente, 1 nueva");
  expect(countsText(0, 3)).toBe("3 nuevas");
});
