import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import PendingPage, { applyFilter, byKind } from "./PendingPage";
import { doubt, doubts, item, question, resolution } from "./testPending";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const TOPIC = "/api/subjects/historia/topics/revolucion-francesa";
const DOUBTS = `${TOPIC}/doubts`;
const NOTES = `${TOPIC}/notes`;
const TOPICS = {
  "/api/subjects/historia/topics": jsonResponse({
    subject_id: "historia",
    topics: [{ topic_id: "revolucion-francesa", subject_id: "historia", name: "La Revolución francesa" }],
  }),
};

function notes(text: string, version = 1) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion-francesa", text, version });
}

const BASE_ROUTES = { ...TOPICS, [NOTES]: notes("# La Revolución francesa\n\nLa Bastilla cayó en [[?1789]].\n") };

it("groups the cards by kind, contradictions first, and shows the open count", async () => {
  stubApi({
    ...BASE_ROUTES,
    [DOUBTS]: jsonResponse(
      doubts([
        doubt({ id: "a", kind: "illegible", text: "Fecha ilegible." }),
        doubt({ id: "b", kind: "contradiction", text: "1789 o 1791." }),
        doubt({ id: "c", kind: "illegible", text: "Nombre ilegible." }),
      ]),
    ),
  });

  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  expect(await screen.findByText("3 dudas por revisar")).toBeInTheDocument();
  const groups = screen.getAllByRole("region").filter((r) => r.getAttribute("aria-label")?.includes("("));
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

it("filters the closed doubts on the page", async () => {
  const fetchMock = stubApi({
    ...BASE_ROUTES,
    [DOUBTS]: jsonResponse(doubts([doubt({ id: "d", status: "dismissed", text: "Ya no importa." })])),
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);
  expect(await screen.findByText("Nada por revisar")).toBeInTheDocument();
  expect(screen.getByText("No hay dudas que mostrar.")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Cerradas" }));

  expect(screen.getByRole("article", { name: "Ilegible: Ya no importa." })).toHaveTextContent("Descartada");
  expect(screen.getByRole("button", { name: "Cerradas" })).toHaveAttribute("aria-pressed", "true");
  expect(fetchMock.mock.calls.filter(([path]) => path === DOUBTS)).toHaveLength(1);
});

it("puts the answer form on the current doubt and lets the student pick another", async () => {
  stubApi({
    ...BASE_ROUTES,
    [DOUBTS]: jsonResponse(
      doubts(
        [
          doubt({ id: "a", text: "Fecha ilegible." }, question({ pending_id: "a" })),
          doubt({ id: "b", kind: "incomplete", text: "Falta la causa." }),
        ],
        "a",
      ),
    ),
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  const first = await screen.findByRole("article", { name: "Ilegible: Fecha ilegible." });
  expect(within(first).getByRole("form", { name: "Resolver la duda" })).toBeInTheDocument();
  const second = screen.getByRole("article", { name: "Incompleto: Falta la causa." });
  fireEvent.click(within(second).getByRole("button", { name: "Resolver esta duda" }));

  expect(within(second).getByRole("form", { name: "Resolver la duda" })).toBeInTheDocument();
  expect(within(first).queryByRole("form")).not.toBeInTheDocument();
});

it("after an answer reads the queue and the changed notes again", async () => {
  let answered = false;
  let notesReads = 0;
  stubApi({
    ...TOPICS,
    [NOTES]: () => {
      notesReads += 1;
      return notes(answered ? "# Tema\n\nLa Bastilla cayó en 1789.\n" : "# Tema\n\nLa Bastilla cayó en [[?1789]].\n", answered ? 2 : 1);
    },
    [DOUBTS]: () =>
      jsonResponse(
        answered
          ? doubts([doubt({ status: "resolved", resolution: "La fecha es el 14 de julio de 1789." })])
          : doubts([doubt({}, question())]),
      ),
    [`POST ${DOUBTS}/p1/answer`]: () => {
      answered = true;
      return jsonResponse(resolution());
    },
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);
  expect(await screen.findByText("1 duda por revisar")).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Apuntes · versión 1" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "14 de julio de 1789" }));

  expect(await screen.findByText("Nada por revisar")).toBeInTheDocument();
  expect(
    screen.getByText("Duda resuelta: La fecha es el 14 de julio de 1789. Los apuntes se han actualizado."),
  ).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Apuntes · versión 2" })).toBeInTheDocument();
  expect(screen.getByText("La Bastilla cayó en 1789.")).toBeInTheDocument();
  expect(notesReads).toBe(2);
});

it("asks the editor for the questions of doubts that have none", async () => {
  let reviewed = false;
  const fetchMock = stubApi({
    ...BASE_ROUTES,
    [DOUBTS]: () => jsonResponse(doubts([reviewed ? doubt({}, question()) : doubt()])),
    [`POST ${DOUBTS}/review`]: () => {
      reviewed = true;
      return jsonResponse({
        subject: "historia",
        topic: "revolucion-francesa",
        auto_resolved: [],
        asked: ["p1"],
        notes_changed: false,
        session_id: "s",
        commit: "c",
        attempts: 1,
        warning: null,
        model: "m",
      });
    },
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  fireEvent.click(await screen.findByRole("button", { name: "Preparar las preguntas" }));

  expect(await screen.findByText("El editor tiene 1 pregunta para ti.")).toBeInTheDocument();
  expect(await screen.findByText("¿Qué fecha pone en la página 1?")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Preparar las preguntas" })).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    `${DOUBTS}/review`,
    expect.objectContaining({ method: "POST", body: JSON.stringify({ confirm_over_cap: false }) }),
  );
});

it("explains a review refused with 409 and confirms past a cost cap", async () => {
  const cap = "Se ha alcanzado el límite de gasto del día (5.10 de 5.00 USD). Confirma para continuar igualmente.";
  let calls = 0;
  const fetchMock = stubApi({
    ...BASE_ROUTES,
    [DOUBTS]: jsonResponse(doubts([doubt()])),
    [`POST ${DOUBTS}/review`]: () =>
      ++calls === 1
        ? jsonResponse({ detail: cap, code: "cost_cap_reached" }, 409)
        : jsonResponse({ auto_resolved: ["p1"], asked: [], notes_changed: true, warning: null }),
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  fireEvent.click(await screen.findByRole("button", { name: "Preparar las preguntas" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(`No se pudieron preparar las preguntas: ${cap}`);
  fireEvent.click(screen.getByRole("button", { name: "Continuar igualmente" }));

  expect(await screen.findByText("El editor ha resuelto 1 duda con tus fuentes.")).toBeInTheDocument();
  const reviews = fetchMock.mock.calls.filter(([path]) => path === `${DOUBTS}/review`);
  expect(JSON.parse((reviews[1][1] as RequestInit).body as string)).toEqual({ confirm_over_cap: true });
});

it("reads the queue again on a timer so the count follows a live session", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let answer = doubts([doubt()]);
  stubApi({ ...BASE_ROUTES, [DOUBTS]: () => jsonResponse(answer) });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" pollMs={1000} />);
  expect(await screen.findByText("1 duda por revisar")).toBeInTheDocument();

  answer = doubts([doubt(), doubt({ id: "p2", kind: "incomplete", text: "Falta la causa." })]);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });

  expect(await screen.findByText("2 dudas por revisar")).toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Incompleto: Falta la causa." })).toBeInTheDocument();
});

it("keeps the last queue when a later read fails", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let fail = false;
  stubApi({ ...BASE_ROUTES, [DOUBTS]: () => (fail ? jsonResponse({}, 500) : jsonResponse(doubts([doubt()]))) });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" pollMs={1000} />);
  expect(await screen.findByText("1 duda por revisar")).toBeInTheDocument();

  fail = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });

  expect(await screen.findByText(/No se pudo actualizar la lista/)).toBeInTheDocument();
  expect(screen.getByText("1 duda por revisar")).toBeInTheDocument();
});

it("explains a first read that fails, and notes that do not exist yet", async () => {
  stubApi({
    ...TOPICS,
    [DOUBTS]: jsonResponse({ detail: "Tema desconocido." }, 404),
    [NOTES]: jsonResponse({ detail: "Todavía no hay apuntes de este tema." }, 404),
  });
  render(<PendingPage subjectId="historia" topicId="revolucion-francesa" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar las dudas: Tema desconocido.");
  await waitFor(() => expect(screen.getByText("Todavía no hay apuntes de este tema.")).toBeInTheDocument());
});

it("puts unknown kinds after the known ones and filters by status", () => {
  const groups = byKind([item({ id: "x", kind: "new_kind" }), item({ id: "y", kind: "incomplete" })]);
  expect(groups.map((g) => g[0].kind)).toEqual(["incomplete", "new_kind"]);
  const all = [doubt({ id: "o" }), doubt({ id: "r", status: "auto_resolved" })];
  expect(applyFilter(all, "open").map((d) => d.item.id)).toEqual(["o"]);
  expect(applyFilter(all, "closed").map((d) => d.item.id)).toEqual(["r"]);
  expect(applyFilter(all, "all")).toHaveLength(2);
});
