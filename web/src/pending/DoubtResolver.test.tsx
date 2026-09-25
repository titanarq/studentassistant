import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import DoubtResolver, { sourceLabel } from "./DoubtResolver";
import { doubt, question, resolution } from "./testPending";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/doubts";

function sent(fetchMock: ReturnType<typeof stubApi>, call = 0): unknown {
  return JSON.parse((fetchMock.mock.calls[call][1] as RequestInit).body as string);
}

function renderResolver(d = doubt({}, question()), props: Partial<Parameters<typeof DoubtResolver>[0]> = {}) {
  const onResolved = vi.fn();
  const onStale = vi.fn();
  render(
    <DoubtResolver
      subjectId="historia"
      topicId="revolucion-francesa"
      doubt={d}
      onResolved={onResolved}
      onStale={onStale}
      {...props}
    />,
  );
  return { onResolved, onStale };
}

it("answers with a suggested answer by its number", async () => {
  const fetchMock = stubApi({ [`POST ${BASE}/p1/answer`]: jsonResponse(resolution()) });
  const { onResolved } = renderResolver();

  expect(screen.getByText("¿Qué fecha pone en la página 1?")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "14 de julio de 1791" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  expect(sent(fetchMock)).toEqual({ suggestion: 2, confirm_over_cap: false });
  expect(onResolved.mock.calls[0][0]).toMatchObject({ pending_id: "p1", notes_changed: true });
});

it("answers with free text, and without a question too", async () => {
  const fetchMock = stubApi({ [`POST ${BASE}/p1/answer`]: jsonResponse(resolution()) });
  const { onResolved } = renderResolver(doubt());

  expect(screen.getByText(/todavía no ha preparado una pregunta/)).toBeInTheDocument();
  const submit = screen.getByRole("button", { name: "Responder" });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Tu respuesta" }), { target: { value: "  Es 1789.  " } });
  fireEvent.click(submit);

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  expect(sent(fetchMock)).toEqual({ answer: "Es 1789.", confirm_over_cap: false });
});

it("picks the right source of a contradiction and keeps a note of the other", async () => {
  const fetchMock = stubApi({ [`POST ${BASE}/p1/answer`]: jsonResponse(resolution()) });
  const contradiction = doubt(
    { kind: "contradiction" },
    question({
      question: "¿Qué año vale?",
      suggestions: [],
      options: [
        { source_id: "sources/notes/page-001.jpg", says: "1789" },
        { source_id: "sources/book/page-004.jpg", says: "1791" },
      ],
    }),
  );
  const { onResolved } = renderResolver(contradiction);

  const keep = screen.getByRole("checkbox", { name: /Guardar también una nota/ });
  expect(keep).toBeDisabled();
  fireEvent.click(screen.getByRole("radio", { name: "Tus apuntes, página 1: «1789»" }));
  fireEvent.click(keep);
  fireEvent.change(screen.getByRole("textbox", { name: "Comentario (opcional)" }), {
    target: { value: "Lo dijo el profesor." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Responder" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  expect(sent(fetchMock)).toEqual({
    source_id: "sources/notes/page-001.jpg",
    keep_discarded: true,
    answer: "Lo dijo el profesor.",
    confirm_over_cap: false,
  });
});

it("dismisses the doubt", async () => {
  const fetchMock = stubApi({ [`POST ${BASE}/p1/dismiss`]: jsonResponse(resolution({ status: "dismissed" })) });
  const { onResolved } = renderResolver();

  fireEvent.click(screen.getByRole("button", { name: "Descartar" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/p1/dismiss`, { method: "POST" });
});

it("shows a closed doubt's 409 in Spanish and asks for the queue again", async () => {
  stubApi({ [`POST ${BASE}/p1/answer`]: jsonResponse({ detail: "Esa duda ya está cerrada." }, 409) });
  const { onResolved, onStale } = renderResolver();

  fireEvent.click(screen.getByRole("button", { name: "14 de julio de 1789" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Esa duda ya está cerrada.");
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).not.toBeInTheDocument();
  expect(onStale).toHaveBeenCalledTimes(1);
  expect(onResolved).not.toHaveBeenCalled();
});

it("repeats the same answer with confirm_over_cap past a reached cost cap", async () => {
  const cap = "Se ha alcanzado el límite de gasto de la sesión (1.20 de 1.00 USD). Confirma para continuar igualmente.";
  let calls = 0;
  const fetchMock = stubApi({
    [`POST ${BASE}/p1/answer`]: () => (++calls === 1 ? jsonResponse({ detail: cap }, 409) : jsonResponse(resolution())),
  });
  const { onResolved, onStale } = renderResolver();

  fireEvent.click(screen.getByRole("button", { name: "14 de julio de 1789" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("límite de gasto de la sesión");
  expect(onStale).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Continuar igualmente" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  expect(sent(fetchMock, 1)).toEqual({ suggestion: 1, confirm_over_cap: true });
});

it("shows the other 409s as the backend words them", async () => {
  const busy = "El editor ya está trabajando en los apuntes o las dudas de este tema.";
  stubApi({ [`POST ${BASE}/p1/dismiss`]: jsonResponse({ detail: busy }, 409) });
  renderResolver();

  fireEvent.click(screen.getByRole("button", { name: "Descartar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(busy);
});

it("waits while another operation of the page runs", () => {
  renderResolver(doubt({}, question()), { disabled: true });
  expect(screen.getByRole("button", { name: "Descartar" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "14 de julio de 1789" })).toBeDisabled();
});

it("names the sources in Spanish", () => {
  expect(sourceLabel("sources/notes/page-003.jpg")).toBe("Tus apuntes, página 3");
  expect(sourceLabel("sources/book/page-012.jpg")).toBe("El libro, página 12");
  expect(sourceLabel("sources/pdf/001-tema.p082.jpg")).toBe("El PDF, página 82");
  expect(sourceLabel("sources/web/001-bastilla.md")).toBe("Una página web");
  expect(sourceLabel("sessions/2026-09-24-1030#t=00:01:00-00:02:00")).toBe("Lo que dijiste en clase");
});
