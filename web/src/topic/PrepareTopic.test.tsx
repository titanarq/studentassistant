import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import PrepareTopic from "./PrepareTopic";

afterEach(() => {
  vi.unstubAllGlobals();
});

const TOPIC = "/api/subjects/historia/topics/revolucion-francesa";
const GENERATE = `POST ${TOPIC}/notes/generate`;
const REVIEW = `POST ${TOPIC}/doubts/review`;
const CAP = "Se ha alcanzado el límite de gasto del día (5.10 de 5.00 USD). Confirma para continuar igualmente.";

function generated(overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    subject: "historia",
    topic: "revolucion-francesa",
    draft: false,
    path: "subjects/historia/topics/revolucion-francesa/notes/apuntes.md",
    version: 3,
    tag: "historia/revolucion-francesa/apuntes-v3",
    commit: "abc",
    attempts: 1,
    errors: [],
    warning: null,
    model: "claude-opus",
    ...overrides,
  });
}

const REVIEWED = jsonResponse({ auto_resolved: ["a", "b"], asked: ["c"], notes_changed: true, warning: null });

function bodies(fetchMock: ReturnType<typeof stubApi>, path: string): unknown[] {
  return fetchMock.mock.calls
    .filter(([p]) => p === path)
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

it("writes the notes and then reviews the doubts", async () => {
  const fetchMock = stubApi({ [GENERATE]: generated(), [REVIEW]: REVIEWED });
  const onDone = vi.fn();
  render(<PrepareTopic subjectId="historia" topicId="revolucion-francesa" onDone={onDone} />);

  fireEvent.click(screen.getByRole("button", { name: "Prepárame el tema" }));

  expect(await screen.findByText("El editor ha resuelto 2 dudas con tus fuentes y tiene 1 pregunta para ti.")).toBeInTheDocument();
  expect(screen.getByText("Apuntes v3 listos.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Ver las dudas" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-francesa/pending",
  );
  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([`${TOPIC}/notes/generate`, `${TOPIC}/doubts/review`]);
  expect(onDone).toHaveBeenCalledTimes(1);
});

it("does not review a draft", async () => {
  const fetchMock = stubApi({ [GENERATE]: generated({ draft: true, version: null, warning: "Faltan citas." }) });
  render(<PrepareTopic subjectId="historia" topicId="revolucion-francesa" />);

  fireEvent.click(screen.getByRole("button", { name: "Prepárame el tema" }));

  expect(await screen.findByText(/borrador que no pasa la revisión. Faltan citas./)).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("confirms past a cost cap on the step that stopped", async () => {
  let reviews = 0;
  const fetchMock = stubApi({
    [GENERATE]: generated(),
    [REVIEW]: () => (++reviews === 1 ? jsonResponse({ detail: CAP, code: "cost_cap_reached" }, 409) : REVIEWED.clone()),
  });
  render(<PrepareTopic subjectId="historia" topicId="revolucion-francesa" />);

  fireEvent.click(screen.getByRole("button", { name: "Prepárame el tema" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(`No se pudieron revisar las dudas: ${CAP}`);
  expect(screen.getByText("Apuntes v3 listos.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continuar igualmente" }));

  expect(await screen.findByText(/ha resuelto 2 dudas/)).toBeInTheDocument();
  expect(bodies(fetchMock, `${TOPIC}/doubts/review`)).toEqual([{ confirm_over_cap: false }, { confirm_over_cap: true }]);
  expect(bodies(fetchMock, `${TOPIC}/notes/generate`)).toEqual([{ confirm_over_cap: false }]);
});

it("shows a generation refused with 409 in Spanish, with no confirm when it is not the cost cap", async () => {
  const busy = "Ya se están preparando los apuntes de este tema.";
  stubApi({ [GENERATE]: jsonResponse({ detail: busy }, 409) });
  render(<PrepareTopic subjectId="historia" topicId="revolucion-francesa" />);

  fireEvent.click(screen.getByRole("button", { name: "Prepárame el tema" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(`No se pudieron preparar los apuntes: ${busy}`);
  await waitFor(() => expect(screen.getByRole("button", { name: "Prepárame el tema" })).toBeEnabled());
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).not.toBeInTheDocument();
});
