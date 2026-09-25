import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import NotesPage from "../notes/NotesPage";
import { NOTES } from "../notes/testNotes";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../test/mockApi";
import { history, revision, turn } from "./testChat";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const CHAT = `${BASE}/notes/chat`;
const EDITED = NOTES.replace(
  "cuando la producción artesanal dio paso a la fábrica.",
  "cuando la producción artesanal dio paso a la fábrica. Por ejemplo, la fábrica textil de Manchester.",
);

const TOPICS = jsonResponse({
  subject_id: "historia",
  topics: [{ topic_id: "revolucion-industrial", subject_id: "historia", name: "La Revolución Industrial" }],
});

function notes(text: string, version: number) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", text, version });
}

/** Answers the notes with `texts` in turn, the last one from then on. */
function notesSequence(...texts: string[]) {
  let read = 0;
  return () => {
    const index = Math.min(read++, texts.length - 1);
    return notes(texts[index], index + 2);
  };
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse((call[1] as RequestInit).body as string);
}

function chatPosts(fetchMock: ReturnType<typeof stubApi>) {
  return fetchMock.mock.calls.filter((call) => call[0] === CHAT && (call[1] as RequestInit | undefined)?.method === "POST");
}

async function renderPage() {
  render(<NotesPage subjectId="historia" topicId="revolucion-industrial" />);
  await screen.findByRole("heading", { name: /Contexto/ });
  return screen.getByRole("region", { name: "Hablar con el editor" });
}

it("streams the reply, shows the applied diff and highlights the changed section", async () => {
  const stream = streamResponse();
  const fetchMock = stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES, EDITED),
    [CHAT]: jsonResponse(history([turn()], false)),
    [`POST ${CHAT}`]: () => stream.response,
  });
  const chat = await renderPage();
  expect(await within(chat).findByText("Pon un ejemplo")).toBeInTheDocument();
  expect(within(chat).getByRole("button", { name: "Deshacer el último cambio" })).toBeDisabled();

  const input = within(chat).getByLabelText("Mensaje para el editor");
  fireEvent.change(input, { target: { value: "Esto está demasiado resumido" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("");
  expect(await within(chat).findByText("El editor está pensando…")).toBeInTheDocument();
  expect(bodyOf(chatPosts(fetchMock)[0])).toEqual({ message: "Esto está demasiado resumido", confirm_over_cap: false });

  await act(async () => stream.push(sseEvent("reply.delta", { text: "He ampliado ", attempt: 1 })));
  expect(await within(chat).findByText(/He ampliado/)).toBeInTheDocument();
  expect(within(chat).getByRole("button", { name: "El editor está respondiendo…" })).toBeDisabled();
  for (const button of screen.getAllByRole("button", { name: "¿Por qué pusiste esto?" })) expect(button).toBeDisabled();

  await act(async () => {
    stream.push(sseEvent("reply.delta", { text: "el contexto", attempt: 1 }));
    stream.push(sseEvent("result", revision()));
    stream.close();
  });
  expect(
    await within(chat).findByText("He ampliado el contexto con tu página 1 y he añadido un ejemplo."),
  ).toBeInTheDocument();
  expect(within(chat).getByText("Cambio aplicado: Contexto ampliado con un ejemplo")).toBeInTheDocument();
  expect(within(chat).getByText(/Cambios en los apuntes \(2 líneas añadidas, 1 quitada\)/)).toBeInTheDocument();
  expect(within(chat).getByText("Por ejemplo, la fábrica textil de Manchester.").closest("ins")).not.toBeNull();
  expect(within(chat).getByText("Resumen corto.").closest("del")).not.toBeNull();

  const edited = await screen.findByText(/la fábrica textil de Manchester\.$/, { selector: "p" });
  expect(edited.closest(".notes-block")).toHaveClass("notes-changed");
  expect(screen.getByText(/Watt mejoró/).closest(".notes-block")).not.toHaveClass("notes-changed");
  expect(screen.getByText(/versión 3/)).toBeInTheDocument();
  expect(within(chat).getByRole("button", { name: "Deshacer el último cambio" })).toBeEnabled();
});

it("drops the streamed reply on a restart and keeps the final one", async () => {
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES),
    [CHAT]: jsonResponse(history()),
    [`POST ${CHAT}`]: () =>
      sseResponse([
        ["reply.delta", { text: "Un intento fallido", attempt: 1 }],
        ["reply.restart", { attempt: 2 }],
        ["result", revision({ reply: "Solo una pregunta: ¿qué página?", applied: false, notes_changed: false, diff: "", commit: null, changed_sections: [] })],
      ]),
  });
  const chat = await renderPage();
  fireEvent.change(within(chat).getByLabelText("Mensaje para el editor"), { target: { value: "¿Qué significa?" } });
  fireEvent.click(within(chat).getByRole("button", { name: "Enviar" }));
  expect(await within(chat).findByText("Solo una pregunta: ¿qué página?")).toBeInTheDocument();
  expect(within(chat).queryByText(/Un intento fallido/)).toBeNull();
  expect(within(chat).queryByText(/Cambios en los apuntes/)).toBeNull();
  expect(within(chat).getByRole("button", { name: "Deshacer el último cambio" })).toBeDisabled();
});

it("undoes the last turn and reads the notes and the conversation again", async () => {
  let historyReads = 0;
  const fetchMock = stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(EDITED, NOTES),
    [CHAT]: () => jsonResponse(historyReads++ === 0 ? history([turn()], true) : history([turn({ undone: true })], false)),
    [`POST ${CHAT}/undo`]: jsonResponse({
      subject: "historia",
      topic: "revolucion-industrial",
      undone_commit: "old111",
      summary: "Ejemplo añadido",
      commit: "def456",
      notes_changed: true,
      diff: "",
      notes: NOTES,
      paths: [],
    }),
  });
  const chat = await renderPage();
  const undo = within(chat).getByRole("button", { name: "Deshacer el último cambio" });
  await waitFor(() => expect(undo).toBeEnabled());
  fireEvent.click(undo);
  expect(await within(chat).findByText("Se ha deshecho el cambio «Ejemplo añadido».")).toBeInTheDocument();
  await waitFor(() => expect(within(chat).getByText(/^Cambio deshecho:/, { selector: ".chat-applied" })).toBeInTheDocument());
  await waitFor(() => expect(undo).toBeDisabled());
  await waitFor(() => expect(screen.queryByText(/Manchester/)).toBeNull());
  expect(fetchMock.mock.calls.filter((call) => call[0] === `${BASE}/notes`)).toHaveLength(2);
});

it("asks the editor why a block is there and opens the sources it points to", async () => {
  const question =
    "¿Por qué pusiste esto? (en la sección #maquina-de-vapor) «Watt mejoró la máquina de Newcomen en 1769. Más detalles en la enciclopedia.»";
  const fetchMock = stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES),
    [CHAT]: jsonResponse(history()),
    [`POST ${BASE}/notes/why`]: () =>
      sseResponse([
        ["reply.delta", { text: "Lo pusiste ", attempt: 1 }],
        [
          "result",
          {
            question,
            reply: "Lo pusiste tú en la página 2.",
            refs: [{ label: "p2", kind: "notes", text: "Apuntes, página 2", source_id: "sources/notes/page-002.jpg" }],
            warning: null,
          },
        ],
      ]),
  });
  const chat = await renderPage();
  const paragraph = screen.getByText(/Watt mejoró/);
  fireEvent.click(within(paragraph.closest(".notes-block") as HTMLElement).getByRole("button", { name: "¿Por qué pusiste esto?" }));
  expect(await within(chat).findByText("Lo pusiste tú en la página 2.")).toBeInTheDocument();
  expect(within(chat).getByText(question)).toBeInTheDocument();
  expect(chatPosts(fetchMock)).toHaveLength(0);
  const [post] = fetchMock.mock.calls.filter((call) => call[0] === `${BASE}/notes/why`);
  const body = bodyOf(post);
  expect(body.section).toBe("maquina-de-vapor");
  expect(body.block).toBe(1);
  expect(body.quote).toBe("Watt mejoró la máquina de Newcomen en 1769. Más detalles en la enciclopedia.");
  expect(body.confirm_over_cap).toBe(false);

  fireEvent.click(within(chat).getByRole("button", { name: "Ver la fuente: Apuntes, página 2" }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});

it("numbers the blocks as the backend does", async () => {
  const fetchMock = stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES),
    [CHAT]: jsonResponse(history()),
    [`POST ${BASE}/notes/why`]: () => sseResponse([["result", { question: "¿Por qué?", reply: "Porque sí.", refs: [], warning: null }]]),
  });
  const chat = await renderPage();
  const ask = async (element: HTMLElement) => {
    fireEvent.click(within(element.closest(".notes-block") as HTMLElement).getByRole("button", { name: "¿Por qué pusiste esto?" }));
    await within(chat).findAllByText("Porque sí.");
    await waitFor(() => expect(within(chat).getByRole("button", { name: "Enviar" })).toBeInTheDocument());
  };
  await ask(screen.getByText(/Apuntes del tema a partir/));
  await ask(screen.getByRole("table"));
  const bodies = fetchMock.mock.calls.filter((call) => call[0] === `${BASE}/notes/why`).map(bodyOf);
  expect(bodies.map((body) => [body.section, body.block])).toEqual([
    [null, 2],
    ["causas", 2],
  ]);
});

it("shows the sources of an explanation read from the history", async () => {
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES),
    [CHAT]: jsonResponse(
      history([
        turn({
          kind: "explain",
          message: "¿Por qué pusiste esto? «Watt»",
          reply: "Sale de tu página 2.",
          applied: false,
          summary: null,
          commit: null,
          refs: [{ label: "p2", kind: "notes", text: "Apuntes, página 2" }],
        }),
      ]),
    ),
  });
  const chat = await renderPage();
  expect(await within(chat).findByText("Sale de tu página 2.")).toBeInTheDocument();
  expect(within(chat).getByRole("button", { name: "Ver la fuente: Apuntes, página 2" })).toBeInTheDocument();
});

it("offers to continue past a reached cost cap", async () => {
  const detail = "Se ha alcanzado el límite de gasto del día (5.00 de 5.00 USD). Confirma para continuar igualmente.";
  let posts = 0;
  const fetchMock = stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES, EDITED),
    [CHAT]: jsonResponse(history()),
    [`POST ${CHAT}`]: () => (posts++ === 0 ? sseResponse([["error", { status: 409, detail, code: "cost_cap_reached" }]]) : sseResponse([["result", revision()]])),
  });
  const chat = await renderPage();
  fireEvent.change(within(chat).getByLabelText("Mensaje para el editor"), { target: { value: "Pon un ejemplo" } });
  fireEvent.click(within(chat).getByRole("button", { name: "Enviar" }));
  expect(await within(chat).findByRole("alert")).toHaveTextContent(detail);
  fireEvent.click(within(chat).getByRole("button", { name: "Continuar igualmente" }));
  expect(await within(chat).findByText("Cambio aplicado: Contexto ampliado con un ejemplo")).toBeInTheDocument();
  expect(within(chat).queryByRole("alert")).toBeNull();
  expect(within(chat).getAllByText("Pon un ejemplo")).toHaveLength(1);
  expect(bodyOf(chatPosts(fetchMock)[1])).toEqual({ message: "Pon un ejemplo", confirm_over_cap: true });
});

it("reads the conversation again when the stream is cut", async () => {
  let historyReads = 0;
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: notesSequence(NOTES, EDITED),
    [CHAT]: () =>
      jsonResponse(historyReads++ === 0 ? history() : history([turn({ message: "Pon un ejemplo", reply: "Hecho." })], true)),
    [`POST ${CHAT}`]: () => sseResponse([["reply.delta", { text: "Hec", attempt: 1 }]]),
  });
  const chat = await renderPage();
  fireEvent.change(within(chat).getByLabelText("Mensaje para el editor"), { target: { value: "Pon un ejemplo" } });
  fireEvent.click(within(chat).getByRole("button", { name: "Enviar" }));
  expect(await within(chat).findByText("Hecho.")).toBeInTheDocument();
  expect(await screen.findByText(/Manchester/)).toBeInTheDocument();
  expect(within(chat).getByRole("button", { name: "Deshacer el último cambio" })).toBeEnabled();
});

it("offers to prepare the topic when there are no notes yet, and shows them once written", async () => {
  let notesReads = 0;
  stubApi({
    "/api/subjects/historia/topics": TOPICS,
    [`${BASE}/notes`]: () =>
      notesReads++ === 0 ? jsonResponse({ detail: "Todavía no hay apuntes de este tema." }, 404) : notes(NOTES, 1),
    [CHAT]: jsonResponse(history()),
    [`POST ${BASE}/notes/generate`]: jsonResponse({ draft: false, version: 1, warning: null }),
    [`POST ${BASE}/doubts/review`]: jsonResponse({ auto_resolved: [], asked: [], notes_changed: false, warning: null }),
  });
  render(<NotesPage subjectId="historia" topicId="revolucion-industrial" />);
  expect(await screen.findByText("Todavía no hay apuntes de este tema.")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Hablar con el editor" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Prepárame el tema" }));
  expect(await screen.findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Hablar con el editor" })).toBeInTheDocument();
});
