import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import PendingPage, { byKind } from "./PendingPage";
import { item, queue } from "./testPending";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/pending?status=";
const TOPICS = {
  "/api/subjects/historia/topics": jsonResponse({
    subject_id: "historia",
    topics: [{ topic_id: "revolucion-francesa", subject_id: "historia", name: "La Revolución francesa" }],
  }),
};

it("groups the cards by kind, contradictions first, and shows the open count", async () => {
  stubApi({
    ...TOPICS,
    [`${BASE}open`]: jsonResponse(
      queue([
        item({ id: "a", kind: "illegible", text: "Fecha ilegible." }),
        item({ id: "b", kind: "contradiction", text: "1789 o 1791." }),
        item({ id: "c", kind: "illegible", text: "Nombre ilegible." }),
      ]),
    ),
  });

  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  expect(await screen.findByText("3 dudas por revisar")).toBeInTheDocument();
  const groups = screen.getAllByRole("region");
  expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["Contradicción (1)", "Ilegible (2)"]);
  expect(within(groups[1]).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual([
    "Ilegible: Fecha ilegible.",
    "Ilegible: Nombre ilegible.",
  ]);
  expect(await screen.findByRole("link", { name: "← Tema La Revolución francesa" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa",
  );
});

it("switches the filter to the closed doubts", async () => {
  const fetchMock = stubApi({
    ...TOPICS,
    [`${BASE}open`]: jsonResponse(queue([], 0)),
    [`${BASE}closed`]: jsonResponse(queue([item({ status: "dismissed", text: "Ya no importa." })], 0)),
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);
  expect(await screen.findByText("Nada por revisar")).toBeInTheDocument();
  expect(screen.getByText("No hay dudas que mostrar.")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Cerradas" }));

  expect(await screen.findByRole("article", { name: "Ilegible: Ya no importa." })).toHaveTextContent("Descartada");
  expect(screen.getByRole("button", { name: "Cerradas" })).toHaveAttribute("aria-pressed", "true");
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}closed`);
});

it("reads the queue again on a timer so the count follows a live session", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let answer = queue([item()]);
  stubApi({ ...TOPICS, [`${BASE}open`]: () => jsonResponse(answer) });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" pollMs={1000} />);
  expect(await screen.findByText("1 duda por revisar")).toBeInTheDocument();

  answer = queue([item(), item({ id: "p2", kind: "incomplete", text: "Falta la causa." })]);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });

  expect(await screen.findByText("2 dudas por revisar")).toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Incompleto: Falta la causa." })).toBeInTheDocument();
});

it("keeps the last queue when a later read fails", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let fail = false;
  stubApi({ ...TOPICS, [`${BASE}open`]: () => (fail ? jsonResponse({}, 500) : jsonResponse(queue([item()]))) });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" pollMs={1000} />);
  expect(await screen.findByText("1 duda por revisar")).toBeInTheDocument();

  fail = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });

  expect(await screen.findByText(/No se pudo actualizar la lista/)).toBeInTheDocument();
  expect(screen.getByText("1 duda por revisar")).toBeInTheDocument();
});

it("explains a first read that fails", async () => {
  stubApi({ ...TOPICS, [`${BASE}open`]: jsonResponse({ detail: "Tema desconocido." }, 404) });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar las dudas: Tema desconocido.");
});

it("puts unknown kinds after the known ones", () => {
  const groups = byKind([item({ id: "x", kind: "new_kind" }), item({ id: "y", kind: "incomplete" })]);
  expect(groups.map((g) => g[0].kind)).toEqual(["incomplete", "new_kind"]);
});
