import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DIFF, history, revision, turn } from "../../chat/testChat";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../../test/mockApi";
import { type WorkspaceState, WorkspaceContext } from "../state";
import WorkspaceChatSlot from "../WorkspaceChatSlot";
import { clock } from "./ChatPanel";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const STREAM = `${BASE}/workspace/stream`;
const CHAT = `${BASE}/notes/chat`;
const MESSAGES = `${BASE}/workspace/messages`;

const TRANSCRIPT = {
  session_id: "s-20260926-1000",
  segment_ids: ["seg-0007", "seg-0008"],
  t_start_ms: 154_000,
  t_end_ms: 190_000,
  text: "vale pues esto ponlo como una tabla con las tres causas del carbón el hierro y la máquina de vapor",
};

function detected(requestId: string, summary: string) {
  return sseEvent("request.detected", { request_id: requestId, kind: "edit", summary, transcript: TRANSCRIPT });
}

function voiceResult(turnId: string, requestId: string, extra: Record<string, unknown> = {}) {
  return revision({
    turn_id: turnId,
    request_id: requestId,
    kind: "revise",
    origin: "voice",
    message: TRANSCRIPT.text,
    reply: "He puesto una tabla con las tres causas.",
    summary: "Tabla de las causas",
    request: { request_id: requestId, summary: "Una tabla con las tres causas", ...TRANSCRIPT },
    ...extra,
  });
}

type Stream = ReturnType<typeof streamResponse>;

function setup(routes: Record<string, Response | (() => Response | Promise<Response>)> = {}, { capturing = false } = {}) {
  const streams: Stream[] = [];
  const reloadNotes = vi.fn(async () => undefined);
  const doubtsChanged = vi.fn();
  const onOpenSource = vi.fn();
  const fetchMock = stubApi({
    [STREAM]: () => {
      const stream = streamResponse();
      streams.push(stream);
      return stream.response;
    },
    [CHAT]: jsonResponse(history()),
    ...routes,
  });
  const state: WorkspaceState = {
    subjectId: "historia",
    topicId: "revolucion-industrial",
    notes: { kind: "empty" },
    changedSections: new Set(),
    reloadNotes,
    doubtsKey: 0,
    doubtsChanged,
  };
  const view = (now: boolean) => (
    <WorkspaceContext.Provider value={state}>
      <WorkspaceChatSlot onOpenSource={onOpenSource} retryDelays={[5]} capturing={now} />
    </WorkspaceContext.Provider>
  );
  const { rerender } = render(view(capturing));
  const setCapturing = (now: boolean) => rerender(view(now));
  const opened = async (count = 1) => {
    await waitFor(() => expect(streams).toHaveLength(count));
    return streams[count - 1];
  };
  const calls = (path: string, method = "GET") =>
    fetchMock.mock.calls.filter(([input, init]) => input === path && ((init as RequestInit | undefined)?.method ?? "GET") === method);
  return { streams, reloadNotes, doubtsChanged, onOpenSource, fetchMock, opened, calls, setCapturing };
}

const log = () => screen.getByRole("log", { name: "Conversación con el asistente" });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("formats session times as hh:mm:ss", () => {
  expect(clock(154_000)).toBe("00:02:34");
  expect(clock(3_723_000)).toBe("01:02:03");
});

it("shows the history, then follows a spoken request live until its result, and reloads the notes", async () => {
  const { opened, reloadNotes } = setup({
    [CHAT]: jsonResponse(history([turn({ turn_id: "turn-old", message: "Pon un ejemplo", reply: "Añadido un ejemplo." })], true)),
  });
  expect(await screen.findByText("Pon un ejemplo")).toBeInTheDocument();
  expect(screen.getByText("Añadido un ejemplo.")).toBeInTheDocument();
  // The log itself is not a live region: only the latest turn is announced (#412).
  expect(log()).toHaveAttribute("aria-live", "off");

  const stream = await opened();
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  const entry = (await screen.findByText(/Pediste: Una tabla con las tres causas/)).closest("li") as HTMLElement;
  expect(within(entry).getByText("En cola…")).toBeInTheDocument();
  expect(within(entry).getByText(/Por voz/)).toBeInTheDocument();

  act(() => stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" })));
  expect(await within(entry).findByText("El asistente está pensando…")).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "He puesto ", attempt: 1 }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "una tabla…", attempt: 1 }));
  });
  expect(await within(entry).findByText("He puesto una tabla…")).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("turn.result", voiceResult("turn-1", "req-1", { changed_sections: ["causas"] })));
    stream.push(sseEvent("notes.changed", { revision: "b".repeat(64), origin: "editor", summary: "Tabla de las causas", turn_id: "turn-1" }));
  });
  expect(await within(entry).findByText("He puesto una tabla con las tres causas.")).toBeInTheDocument();
  expect(within(entry).getByText(/Cambio aplicado: Tabla de las causas/)).toBeInTheDocument();
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledWith(["causas"]));

  const toggle = within(entry).getByRole("button", { name: "Ver los cambios" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(within(entry).getByText(/Cambios en los apuntes/)).toBeInTheDocument();
  expect(within(entry).getByRole("button", { name: "Ocultar los cambios" })).toHaveAttribute("aria-expanded", "true");
  // One entry per request: the queued line became the turn.
  expect(within(log()).getAllByRole("listitem")).toHaveLength(2);
});

it("expands and collapses the raw transcript of a spoken request", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  const more = await screen.findByRole("button", { name: "Ver lo que dijiste" });
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(/vale pues esto ponlo/)).toBeNull();

  fireEvent.click(more);
  expect(more).toHaveAttribute("aria-expanded", "true");
  expect(more).toHaveAccessibleName("Ocultar lo que dijiste");
  expect(screen.getByText(`«${TRANSCRIPT.text}»`)).toBeInTheDocument();
  expect(screen.getByText("00:02:34–00:03:10")).toBeInTheDocument();
  expect(document.getElementById(more.getAttribute("aria-controls") ?? "")).not.toBeNull();

  fireEvent.click(more);
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(/vale pues esto ponlo/)).toBeNull();
});

it("keeps the order of several queued requests", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(detected("req-1", "Primera petición"));
    stream.push(detected("req-2", "Segunda petición"));
    stream.push(detected("req-3", "Tercera petición"));
  });
  await screen.findByText(/Tercera petición/);
  const items = within(log()).getAllByRole("listitem");
  expect(items.map((item) => within(item).getByText(/Pediste:/).textContent)).toEqual([
    "Pediste: Primera petición …",
    "Pediste: Segunda petición …",
    "Pediste: Tercera petición …",
  ]);
  expect(screen.getAllByText("En cola…")).toHaveLength(3);

  act(() => stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" })));
  expect(await within(items[0]).findByText("El asistente está pensando…")).toBeInTheDocument();
  expect(screen.getAllByText("En cola…")).toHaveLength(2);
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function posted(requests: Record<string, unknown>[], messageId = "msg-0123abcd") {
  return jsonResponse({ message_id: messageId, requests, classified: true }, 202);
}

function typedRequest(requestId: string, kind: string, summary: string, text: string, extra: Record<string, unknown> = {}) {
  return { request_id: requestId, kind, summary, text, t_start_ms: 0, t_end_ms: 0, detector: "typed", ...extra };
}

async function type(text: string) {
  const input = screen.getByLabelText("Mensaje para el asistente");
  fireEvent.change(input, { target: { value: text } });
  fireEvent.keyDown(input, { key: "Enter" });
}

it("sends a typed message to the classifier with Enter, queues its requests and follows their turns", async () => {
  const answer = deferred<Response>();
  const { opened, calls, reloadNotes } = setup({ [`POST ${MESSAGES}`]: () => answer.promise });
  const stream = await opened();

  const input = screen.getByLabelText("Mensaje para el asistente");
  const send = screen.getByRole("button", { name: "Enviar" });
  expect(send).toBeDisabled();
  fireEvent.change(input, { target: { value: "Pon un ejemplo y aparta la 9" } });
  expect(send).toBeEnabled();
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(calls(MESSAGES, "POST")).toHaveLength(0);
  fireEvent.keyDown(input, { key: "Enter" });

  await waitFor(() => expect(calls(MESSAGES, "POST")).toHaveLength(1));
  expect(JSON.parse(String((calls(MESSAGES, "POST")[0][1] as RequestInit).body))).toEqual({ text: "Pon un ejemplo y aparta la 9" });
  expect(calls(CHAT, "POST")).toHaveLength(0);
  expect(input).toHaveValue("");
  expect(await within(log()).findByText("Enviando…")).toBeInTheDocument();

  await act(async () =>
    answer.resolve(
      posted([
        typedRequest("req-t1", "edit", "Un ejemplo", "Pon un ejemplo y aparta la 9"),
        typedRequest("req-t2", "set_aside", "Apartar la página 9", "Pon un ejemplo y aparta la 9", { targets: ["sources/notes/page-009.jpg"] }),
      ]),
    ),
  );
  expect(await within(log()).findAllByText("En cola…")).toHaveLength(2);
  expect(screen.queryByText("Enviando…")).toBeNull();
  const [first, second] = within(log()).getAllByRole("listitem");
  expect(within(first).getByText("Escribiste")).toBeInTheDocument();
  expect(within(first).getByText("Pon un ejemplo y aparta la 9")).toBeInTheDocument();
  expect(within(second).getByText("Y además")).toBeInTheDocument();
  expect(within(second).getByText("Apartar la página 9")).toBeInTheDocument();

  act(() => {
    // The stream's own announcement of a request already shown adds nothing.
    stream.push(sseEvent("request.detected", { request_id: "req-t1", kind: "edit", summary: "Un ejemplo", origin: "typed", transcript: { text: "Pon un ejemplo y aparta la 9" } }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-9", request_id: "req-t1", origin: "typed", kind: "revise" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-9", text: "Añado ", attempt: 1 }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-9", text: "un ejemplo.", attempt: 1 }));
  });
  expect(await within(first).findByText("Añado un ejemplo.")).toBeInTheDocument();
  expect(within(second).getByText("En cola…")).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("turn.result", { ...revision({ turn_id: "turn-9", message: "Pon un ejemplo y aparta la 9", reply: "Añado un ejemplo de Manchester." }), request_id: "req-t1", kind: "revise" }));
    stream.push(sseEvent("notes.changed", { revision: "b".repeat(64), origin: "editor", summary: "Ejemplo", turn_id: "turn-9" }));
  });
  expect(await within(first).findByText("Añado un ejemplo de Manchester.")).toBeInTheDocument();
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledWith(["contexto"]));
  expect(within(log()).getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getAllByText("Añado un ejemplo de Manchester.")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Deshacer el último cambio" })).toBeEnabled();
});

it("shows a set-aside or restore request as one short line", async () => {
  const { opened } = setup({
    [`POST ${MESSAGES}`]: () =>
      posted([typedRequest("req-t1", "set_aside", "Apartar la página 9", "aparta la 9", { targets: ["sources/notes/page-009.jpg"] })]),
  });
  const stream = await opened();
  await type("aparta la 9");
  const entry = (await within(log()).findByText("aparta la 9")).closest("li") as HTMLElement;
  expect(await within(entry).findByText("En cola…")).toBeInTheDocument();

  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-3", request_id: "req-t1", origin: "typed", kind: "set_aside" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-3", text: "He apartado la página 9.", attempt: 1 }));
    stream.push(
      sseEvent("turn.result", {
        turn_id: "turn-3",
        request_id: "req-t1",
        kind: "set_aside",
        origin: "typed",
        request: null,
        decision: "set_aside",
        source_ids: ["sources/notes/page-009.jpg"],
        message: "aparta la 9",
        reply: "He apartado la página 9.",
        applied: true,
      }),
    );
  });
  expect(await within(entry).findByText("He apartado la página 9.")).toHaveClass("ws-chat-line");
  expect(within(entry).queryByText("Asistente")).toBeNull();
  expect(within(entry).queryByText(/Cambio aplicado/)).toBeNull();
});

it("says why each page was set aside, from the triage turn's reasons", async () => {
  const { opened } = setup({
    [CHAT]: jsonResponse(
      history([
        turn({
          time: "2026-09-25T10:02:00Z",
          kind: "triage",
          turn_id: "turn-old",
          message: "recupera la 2",
          reply: "He recuperado la página 2.",
          summary: "restore",
          source_ids: ["sources/notes/page-002.jpg"],
          targets: [],
          commit: null,
        }),
      ]),
    ),
  });
  const stream = await opened();
  expect(await screen.findByText("He recuperado la página 2.")).toHaveClass("ws-chat-line");
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-5", kind: "set_aside", summary: "Apartar la 9, la 4 y la 1", origin: "voice", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-5", request_id: "req-5", origin: "voice", kind: "set_aside" }));
    stream.push(
      sseEvent("turn.result", {
        turn_id: "turn-5",
        request_id: "req-5",
        kind: "set_aside",
        origin: "voice",
        request: { request_id: "req-5", summary: "Apartar la 9, la 4 y la 1", ...TRANSCRIPT },
        decision: "set_aside",
        source_ids: ["sources/notes/page-009.jpg", "sources/notes/page-004.jpg"],
        targets: [
          { source_id: "sources/notes/page-009.jpg", reasons: ["blank"], duplicate_of: null, already: false },
          { source_id: "sources/notes/page-004.jpg", reasons: [], duplicate_of: null, already: false },
          { source_id: "sources/notes/page-001.jpg", reasons: ["duplicate"], duplicate_of: "sources/notes/page-002.jpg", already: true },
        ],
        message: TRANSCRIPT.text,
        reply: "He apartado la página 9 (página en blanco) y la página 4. La página 1 (repetida de la página 2) ya estaba apartada.",
        applied: true,
      }),
    );
  });
  const entry = (await screen.findByText(/Pediste: Apartar la 9/)).closest("li") as HTMLElement;
  const lines = await within(entry).findAllByText(/apartada/);
  expect(lines.map((line) => line.textContent)).toEqual([
    "Página 9 apartada: en blanco",
    "Página 4 apartada",
    "Página 1 ya estaba apartada: repetida de la página 2",
  ]);
  lines.forEach((line) => expect(line).toHaveClass("ws-chat-line"));
  expect(within(entry).queryByText(/He apartado/)).toBeNull();
});

it("says so when a typed message is refused", async () => {
  setup({
    [`POST ${MESSAGES}`]: jsonResponse({ detail: "El asistente no está disponible: falta la conexión con Claude." }, 503),
  });
  await type("Hola");
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo completar: El asistente no está disponible: falta la conexión con Claude.");
  expect(screen.getByText("Hola")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enviar" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).toBeNull();
});

function incorporation(turnId: string, requestId: string | null, extra: Record<string, unknown> = {}) {
  return {
    ...revision({
      turn_id: turnId,
      message: "Incorpora las páginas 3 y 4",
      reply: "He añadido las causas de las páginas 3 y 4.",
      summary: "Causas de las páginas 3 y 4",
    }),
    request_id: requestId,
    kind: "incorporate",
    origin: "voice",
    source_ids: ["sources/notes/page-003.jpg", "sources/notes/page-004.jpg"],
    doubts: ["p-000004", "p-000005"],
    nothing_new: [],
    ...extra,
  };
}

it("shows an incorporation with its sources, its change and the doubts it raised", async () => {
  const { opened, onOpenSource } = setup();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-4", kind: "incorporate", summary: "Incorporar las páginas 3 y 4", origin: "voice", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-4", request_id: "req-4", origin: "voice", kind: "incorporate" }));
  });
  const entry = (await screen.findByText(/Pediste: Incorporar las páginas 3 y 4/)).closest("li") as HTMLElement;
  expect(await within(entry).findByText("Incorporando…")).toBeInTheDocument();

  act(() => stream.push(sseEvent("turn.result", incorporation("turn-4", "req-4"))));
  const line = await within(entry).findByText(/Incorporadas:/);
  expect(line).toHaveTextContent("Incorporadas: página 3, página 4");
  expect(within(entry).getByText("He añadido las causas de las páginas 3 y 4.")).toBeInTheDocument();
  expect(within(entry).getByText(/Cambio aplicado: Causas de las páginas 3 y 4/)).toBeInTheDocument();
  expect(within(entry).getByText("Han surgido 2 dudas: te las pregunto aquí, de una en una.")).toBeInTheDocument();
  fireEvent.click(within(entry).getByRole("button", { name: "Ver los cambios" }));
  expect(within(entry).getByText(/Cambios en los apuntes/)).toBeInTheDocument();

  fireEvent.click(within(line).getByRole("button", { name: "Ver la fuente: página 4" }));
  expect(onOpenSource).toHaveBeenCalledWith(
    "recurso-notes-page-004.jpg",
    expect.any(HTMLElement),
    "[Apuntes, página 4](../sources/notes/page-004.jpg)",
  );
  expect(screen.getByRole("button", { name: "Deshacer el último cambio" })).toBeEnabled();
});

it("shows a whole-topic run as one entry with its progress and each batch below it", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-5", kind: "prepare_notes", summary: "Preparar el tema", origin: "voice", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-run", request_id: "req-5", origin: "voice", kind: "prepare_notes" }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-b1", request_id: null, origin: "typed", kind: "incorporate" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-b1", text: "Añado el contexto…", attempt: 1 }));
  });
  const run = (await screen.findByText(/Pediste: Preparar el tema/)).closest("li") as HTMLElement;
  const batches = await within(run).findByRole("list", { name: "Tandas de la preparación" });
  expect(await within(batches).findByText("Añado el contexto…")).toBeInTheDocument();

  act(() => {
    stream.push(
      sseEvent(
        "turn.result",
        incorporation("turn-b1", null, {
          origin: "typed",
          source_ids: ["sources/notes/page-001.jpg", "sources/notes/page-002.jpg"],
          reply: "He añadido el contexto.",
          doubts: [],
        }),
      ),
    );
    stream.push(sseEvent("incorporation.progress", { done: 2, total: 8, source_ids: ["sources/notes/page-001.jpg", "sources/notes/page-002.jpg"] }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-b2", request_id: null, origin: "typed", kind: "incorporate" }));
  });
  expect(await within(run).findByText("2 de 8 páginas")).toBeInTheDocument();
  expect(within(batches).getByText(/Incorporadas:/)).toHaveTextContent("Incorporadas: página 1, página 2");
  expect(within(batches).getByText("He añadido el contexto.")).toBeInTheDocument();
  expect(await within(batches).findAllByRole("listitem")).toHaveLength(2);
  // One entry for the run: the batches are inside it.
  expect(within(log()).getAllByRole("listitem").filter((li) => li.parentElement === log())).toHaveLength(1);

  act(() => {
    stream.push(sseEvent("turn.result", incorporation("turn-b2", null, { origin: "typed", source_ids: ["sources/notes/page-003.jpg"], doubts: [] })));
    stream.push(sseEvent("incorporation.progress", { done: 8, total: 8, source_ids: ["sources/notes/page-003.jpg"] }));
    stream.push(sseEvent("turn.result", { turn_id: "turn-run", request_id: "req-5", kind: "prepare_notes", version: 3, draft: false, warning: null }));
  });
  expect(await within(run).findByText("He preparado los apuntes del tema (versión 3).")).toBeInTheDocument();
  expect(within(run).getByText("8 de 8 páginas")).toBeInTheDocument();
});

it("gives a run started elsewhere an entry of its own", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-b1", request_id: null, origin: "typed", kind: "incorporate" }));
    stream.push(sseEvent("turn.result", incorporation("turn-b1", null, { origin: "typed", doubts: [] })));
    stream.push(sseEvent("incorporation.progress", { done: 2, total: 2, source_ids: [] }));
  });
  const run = (await screen.findByText("Preparación del tema")).closest("li") as HTMLElement;
  expect(await within(run).findByText("2 de 2 páginas")).toBeInTheDocument();
  expect(within(run).getByText(/Incorporadas:/)).toHaveTextContent("Incorporadas: página 3, página 4");
  expect(run).not.toHaveAttribute("aria-busy");
});

const ASKED = {
  pending_id: "p-000004",
  question: "En la página 3 no leo bien una palabra: ¿«escrita» o «escrito»?",
  suggestions: ["escrita", "escrito"],
  options: [],
  refs: ["sources/notes/page-003.jpg"],
};

it("asks a doubt in the chat, takes the typed answer and marks it answered", async () => {
  const { opened, doubtsChanged, calls } = setup({
    [`POST ${MESSAGES}`]: () => posted([typedRequest("req-t1", "doubt_answer", "Responder la duda", "la 2", { pending_id: "p-000004", answer: "2" })]),
  });
  const stream = await opened();
  const input = screen.getByLabelText("Mensaje para el asistente");
  expect(input).toHaveAttribute("placeholder", "Escribe: «pon un ejemplo aquí»…");

  act(() => stream.push(sseEvent("doubt.asked", ASKED)));
  const doubt = (await screen.findByText(ASKED.question)).closest("li") as HTMLElement;
  expect(doubt).toHaveClass("ws-chat-entry-doubt");
  expect(within(doubt).getByText("Duda")).toBeInTheDocument();
  const suggestions = within(doubt).getByRole("list", { name: "Sugerencias" });
  expect(suggestions.tagName).toBe("OL");
  expect(within(suggestions).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["escrita", "escrito"]);
  expect(within(doubt).getByRole("button", { name: "Ver la fuente: página 3" })).toBeInTheDocument();
  // No answer buttons: it is answered by typing or saying it.
  expect(within(doubt).queryByRole("button", { name: /escrit/ })).toBeNull();
  expect(input).toHaveAttribute("placeholder", "Responde a la duda o escribe otra cosa…");
  expect(doubtsChanged).toHaveBeenCalledTimes(1);

  await type("la 2");
  await waitFor(() => expect(calls(MESSAGES, "POST")).toHaveLength(1));
  const answer = (await within(log()).findByText("la 2")).closest("li") as HTMLElement;
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-7", request_id: "req-t1", origin: "typed", kind: "doubt_answer" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-7", text: "Pone «escrito».", attempt: 1 }));
    stream.push(
      sseEvent("turn.result", {
        subject: "historia",
        topic: "revolucion-industrial",
        pending_id: "p-000004",
        status: "resolved",
        resolution: "Pone «escrito».",
        notes_changed: true,
        revision: "c".repeat(64),
        turn_id: "turn-7",
        request_id: "req-t1",
        kind: "doubt_answer",
      }),
    );
    stream.push(sseEvent("doubt.resolved", { pending_id: "p-000004", status: "resolved", resolution: "Pone «escrito».", notes_changed: true }));
  });
  expect(await within(answer).findByText("Pone «escrito».")).toBeInTheDocument();
  expect(await within(doubt).findByText("Respondida: Pone «escrito».")).toBeInTheDocument();
  expect(within(doubt).getByText("Duda resuelta")).toBeInTheDocument();
  expect(input).toHaveAttribute("placeholder", "Escribe: «pon un ejemplo aquí»…");
  expect(doubtsChanged).toHaveBeenCalledTimes(2);
});

it("shows a contradiction's options with their sources, which open in Recursos", async () => {
  const { opened, onOpenSource } = setup();
  const stream = await opened();
  act(() =>
    stream.push(
      sseEvent("doubt.asked", {
        pending_id: "p-000009",
        question: "Tus apuntes y el libro no dicen lo mismo del año: ¿cuál es?",
        suggestions: [],
        options: [
          { source_id: "sources/notes/page-003.jpg", says: "1760" },
          { source_id: "sources/book/page-083.jpg", says: "1780" },
        ],
        refs: ["sources/notes/page-003.jpg", "sources/book/page-083.jpg"],
      }),
    ),
  );
  const options = await screen.findByRole("list", { name: "Qué dice cada fuente" });
  expect(within(options).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    "Página 3: «1760»",
    "Página 83 del libro: «1780»",
  ]);
  fireEvent.click(within(options).getByRole("button", { name: "Ver la fuente: página 83 del libro" }));
  expect(onOpenSource).toHaveBeenCalledWith(
    "recurso-book-page-083.jpg",
    expect.any(HTMLElement),
    "[Libro, página 83](../sources/book/page-083.jpg)",
  );
  // The refs the options already name are not repeated.
  expect(screen.queryByText(/Sobre:/)).toBeNull();
});

it("merges the new turn kinds of the history after a reconnect without duplicates", async () => {
  let reads = 0;
  const later = [
    turn({
      kind: "incorporate",
      turn_id: "turn-4",
      origin: "voice",
      request_summary: "Incorporar las páginas 3 y 4",
      transcript: { request_id: "req-4", summary: "Incorporar las páginas 3 y 4", ...TRANSCRIPT },
      message: TRANSCRIPT.text,
      reply: "He añadido las causas de las páginas 3 y 4.",
      summary: "Causas de las páginas 3 y 4",
      commit: "inc444",
      source_ids: ["sources/notes/page-003.jpg", "sources/notes/page-004.jpg"],
      diff: DIFF,
    }),
    turn({
      time: "2026-09-25T10:01:00Z",
      kind: "doubt",
      message: "",
      reply: ASKED.question,
      pending_id: ASKED.pending_id,
      question: ASKED.question,
      suggestions: ASKED.suggestions,
      options: [],
      doubt_refs: ASKED.refs,
      status: "resolved",
      resolution: "Pone «escrito».",
      answer: "2",
      applied: true,
      commit: null,
      summary: null,
    }),
    turn({
      time: "2026-09-25T10:02:00Z",
      kind: "triage",
      turn_id: "turn-8",
      message: "aparta la 9",
      reply: "He apartado la página 9.",
      summary: "set_aside",
      source_ids: ["sources/notes/page-009.jpg"],
      commit: null,
    }),
    turn({ time: "2026-09-25T10:03:00Z", kind: "doubts_resolved", message: "", reply: "He resuelto 2 dudas con las fuentes.", pending_ids: ["p-000005", "p-000006"], commit: null, summary: null }),
  ];
  const { opened, calls } = setup({
    [CHAT]: () => jsonResponse(history(reads++ === 0 ? [] : later, true)),
    [`POST ${MESSAGES}`]: () => posted([typedRequest("req-t1", "doubt_answer", "Responder la duda", "la 2", { pending_id: ASKED.pending_id, answer: "2" })]),
  });
  const first = await opened(1);
  await waitFor(() => expect(calls(CHAT)).toHaveLength(1));
  act(() => {
    first.push(sseEvent("request.detected", { request_id: "req-4", kind: "incorporate", summary: "Incorporar las páginas 3 y 4", origin: "voice", transcript: TRANSCRIPT }));
    first.push(sseEvent("turn.started", { turn_id: "turn-4", request_id: "req-4", origin: "voice", kind: "incorporate" }));
    first.push(sseEvent("turn.result", incorporation("turn-4", "req-4", { commit: "inc444" })));
    first.push(sseEvent("doubt.asked", ASKED));
  });
  const doubt = (await screen.findByText(ASKED.question)).closest("li") as HTMLElement;
  await type("la 2");
  const answer = (await within(log()).findByText("la 2")).closest("li") as HTMLElement;
  act(() => {
    first.push(sseEvent("turn.started", { turn_id: "turn-7", request_id: "req-t1", origin: "typed", kind: "doubt_answer" }));
    first.push(sseEvent("turn.result", { pending_id: ASKED.pending_id, status: "resolved", resolution: "Pone «escrito».", notes_changed: true, turn_id: "turn-7", request_id: "req-t1", kind: "doubt_answer" }));
  });
  expect(await within(answer).findByText("Pone «escrito».")).toBeInTheDocument();

  // The doubt.resolved and the rest happen while the stream is down.
  act(() => first.fail());
  await opened(2);
  await waitFor(() => expect(calls(CHAT)).toHaveLength(2));
  expect(await screen.findByText("He apartado la página 9.")).toBeInTheDocument();
  expect(within(log()).getAllByRole("listitem").filter((li) => li.parentElement === log())).toHaveLength(4);
  expect(screen.getAllByText(ASKED.question)).toHaveLength(1);
  // The same element: the doubt is not announced again.
  expect(screen.getByText(ASKED.question).closest("li")).toBe(doubt);
  expect(within(doubt).getByText("Respondiste: «2»")).toBeInTheDocument();
  expect(within(doubt).getByText("Respondida: Pone «escrito».")).toBeInTheDocument();
  // The answer's own entry went: the doubt says what was answered.
  expect(answer).not.toBeInTheDocument();
  const incorporated = screen.getByText(/Incorporadas:/).closest("li") as HTMLElement;
  expect(incorporated).toHaveTextContent("Incorporadas: página 3, página 4");
  // A live entry keeps the diff it received.
  expect(within(incorporated).getByRole("button", { name: "Ver los cambios" })).toBeInTheDocument();
  expect(screen.getByText("He resuelto 2 dudas con las fuentes.")).toHaveClass("ws-chat-line");
  expect(screen.getByLabelText("Mensaje para el asistente")).toHaveAttribute("placeholder", "Escribe: «pon un ejemplo aquí»…");
});

it("drops the reply streamed so far on reply.restart", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: null, origin: "typed", kind: "revise" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "Primer intento", attempt: 1 }));
  });
  expect(await screen.findByText("Primer intento")).toBeInTheDocument();
  act(() => stream.push(sseEvent("reply.restart", { turn_id: "turn-1", attempt: 2 })));
  await waitFor(() => expect(screen.queryByText("Primer intento")).toBeNull());
  act(() => {
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "Stale", attempt: 1 }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "Segundo intento", attempt: 2 }));
  });
  expect(await screen.findByText("Segundo intento")).toBeInTheDocument();
  expect(screen.queryByText(/Stale/)).toBeNull();
});

const OVER_CAP = { status: 409, detail: "Se ha alcanzado el límite de gasto del tema.", code: "cost_cap_reached" };

it("shows a turn error in Spanish and goes on over the cost cap through workspace/messages", async () => {
  const { opened, calls } = setup({
    [`POST ${MESSAGES}`]: () => posted([{ request_id: "req-1", kind: "edit", summary: "Una tabla con las tres causas", text: TRANSCRIPT.text }]),
  });
  const stream = await opened();
  act(() => {
    stream.push(detected("req-1", "Una tabla con las tres causas"));
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("turn.error", { turn_id: "turn-1", request_id: "req-1", ...OVER_CAP }));
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo completar: Se ha alcanzado el límite de gasto del tema.");

  fireEvent.click(screen.getByRole("button", { name: "Continuar igualmente" }));
  await waitFor(() => expect(calls(MESSAGES, "POST")).toHaveLength(1));
  expect(JSON.parse(String((calls(MESSAGES, "POST")[0][1] as RequestInit).body))).toEqual({ confirm_over_cap: true, turn_id: "turn-1" });
  expect(calls(CHAT, "POST")).toHaveLength(0);
  expect(await screen.findByText("En cola…")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();

  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-2", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("turn.result", voiceResult("turn-2", "req-1")));
  });
  expect(await screen.findByText("He puesto una tabla con las tres causas.")).toBeInTheDocument();
  expect(within(log()).getAllByRole("listitem")).toHaveLength(1);
});

it("offers and confirms an incorporation stopped at the cost cap, end to end", async () => {
  const { opened, calls, reloadNotes } = setup({
    [`POST ${MESSAGES}`]: (() => {
      let posts = 0;
      return () =>
        posts++ === 0
          ? posted([typedRequest("req-t1", "incorporate", "Incorporar las páginas 3 y 4", "incorpora la 3 y la 4", { targets: ["sources/notes/page-003.jpg", "sources/notes/page-004.jpg"] })])
          : posted([typedRequest("req-t1", "incorporate", "Incorporar las páginas 3 y 4", "incorpora la 3 y la 4")]);
    })(),
  });
  const stream = await opened();
  await type("incorpora la 3 y la 4");
  const entry = (await within(log()).findByText("incorpora la 3 y la 4")).closest("li") as HTMLElement;
  expect(await within(entry).findByText("En cola…")).toBeInTheDocument();
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-4", request_id: "req-t1", origin: "typed", kind: "incorporate" }));
    stream.push(sseEvent("turn.error", { turn_id: "turn-4", request_id: "req-t1", ...OVER_CAP }));
  });
  const go = await within(entry).findByRole("button", { name: "Continuar igualmente" });
  fireEvent.click(go);
  await waitFor(() => expect(calls(MESSAGES, "POST")).toHaveLength(2));
  expect(JSON.parse(String((calls(MESSAGES, "POST")[1][1] as RequestInit).body))).toEqual({ confirm_over_cap: true, turn_id: "turn-4" });
  expect(calls(CHAT, "POST")).toHaveLength(0);

  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-5", request_id: "req-t1", origin: "typed", kind: "incorporate" }));
    stream.push(sseEvent("turn.result", incorporation("turn-5", "req-t1")));
    stream.push(sseEvent("notes.changed", { revision: "e".repeat(64), origin: "editor", summary: "Causas", turn_id: "turn-5" }));
  });
  expect(await within(entry).findByText("He añadido las causas de las páginas 3 y 4.")).toBeInTheDocument();
  expect(within(entry).getByText(/Incorporadas:/)).toHaveTextContent("Incorporadas: página 3, página 4");
  expect(within(entry).queryByRole("alert")).toBeNull();
  expect(within(log()).getAllByRole("listitem")).toHaveLength(1);
  await waitFor(() => expect(reloadNotes).toHaveBeenCalled());
});

it("says so when a confirmation is refused", async () => {
  const { opened } = setup({
    [`POST ${MESSAGES}`]: jsonResponse({ detail: "Esa petición ya no está esperando confirmación: vuelve a pedirla." }, 404),
  });
  const stream = await opened();
  act(() => {
    stream.push(detected("req-1", "Una tabla"));
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("turn.error", { turn_id: "turn-1", request_id: "req-1", ...OVER_CAP }));
  });
  fireEvent.click(await screen.findByRole("button", { name: "Continuar igualmente" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("vuelve a pedirla");
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).toBeNull();
});

it("shows an error without the cap offer for other failures", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() =>
    stream.push(sseEvent("turn.error", { turn_id: "turn-1", request_id: null, status: 502, detail: "Claude no respondió." })),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("Claude no respondió.");
  expect(screen.queryByRole("button", { name: "Continuar igualmente" })).toBeNull();
});

it("reconnects after a drop, reloads the history and the notes, and never duplicates a turn", async () => {
  let reads = 0;
  const done = turn({
    turn_id: "turn-1",
    origin: "voice",
    request_summary: "Una tabla con las tres causas",
    transcript: { request_id: "req-1", summary: "Una tabla con las tres causas", ...TRANSCRIPT },
    message: TRANSCRIPT.text,
    reply: "He puesto una tabla con las tres causas.",
    summary: "Tabla de las causas",
    commit: "abc123",
  });
  const { opened, calls, reloadNotes } = setup({
    [CHAT]: () => jsonResponse(history(reads++ === 0 ? [] : [done], reads > 1)),
  });
  const first = await opened(1);
  await waitFor(() => expect(calls(CHAT)).toHaveLength(1));
  act(() => {
    first.push(detected("req-1", "Una tabla con las tres causas"));
    first.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    first.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "He puesto", attempt: 1 }));
  });
  expect(await screen.findByText("He puesto")).toBeInTheDocument();

  act(() => first.fail());
  expect(await screen.findByRole("status")).toHaveTextContent("Sin conexión en directo");
  await opened(2);
  await waitFor(() => expect(calls(CHAT)).toHaveLength(2));
  expect(await screen.findByText("He puesto una tabla con las tres causas.")).toBeInTheDocument();
  expect(reloadNotes).toHaveBeenCalled();
  expect(within(log()).getAllByRole("listitem")).toHaveLength(1);
  expect(screen.queryByRole("status")).toBeNull();
  // A history change has no diff: it links to the versions page instead.
  expect(screen.getByRole("link", { name: "Ver las versiones" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-industrial/versions",
  );
});

it("reloads the notes on every notes.changed of the stream", async () => {
  const { opened, reloadNotes } = setup();
  const stream = await opened();
  act(() => stream.push(sseEvent("notes.changed", { revision: "c".repeat(64), origin: "user", summary: null })));
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledWith([]));
  act(() => stream.push(sseEvent("notes.changed", { revision: "d".repeat(64), origin: "restore", summary: "v2" })));
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledTimes(2));
});

it("keeps the undo of the latest applied change", async () => {
  const { calls, reloadNotes } = setup({
    [CHAT]: jsonResponse(history([turn({ turn_id: "turn-old", commit: "old111", summary: "Ejemplo añadido" })], true)),
    [`POST ${CHAT}/undo`]: jsonResponse({ undone_commit: "old111", summary: "Ejemplo añadido", commit: "new222", notes_changed: true, diff: DIFF }),
  });
  const undo = await screen.findByRole("button", { name: "Deshacer el último cambio" });
  await waitFor(() => expect(undo).toBeEnabled());
  fireEvent.click(undo);
  expect(await screen.findByText("Se ha deshecho el cambio «Ejemplo añadido».")).toBeInTheDocument();
  expect(calls(`${CHAT}/undo`, "POST")).toHaveLength(1);
  expect(reloadNotes).toHaveBeenCalledWith([]);
});

function studyTurn(turnId: string, requestId: string, extra: Record<string, unknown> = {}) {
  return {
    turn_id: turnId,
    request_id: requestId,
    kind: "study",
    origin: "voice",
    request: { request_id: requestId, summary: "Dar el tema por terminado y ponerte a estudiar", ...TRANSCRIPT },
    message: TRANSCRIPT.text,
    reply: "He cerrado la captura y marcado los apuntes v5 como versión de estudio.",
    action: { kind: "go_study", path: "/subjects/historia/topics/revolucion-industrial/study" },
    study: {
      subject: "historia",
      topic: "revolucion-industrial",
      study_version: { version: 5, tag: "historia/revolucion-industrial/apuntes-v5", marked_at: "2026-09-26T18:58:00Z" },
      study_current: true,
      options: [],
      created_tag: false,
      ended_session: "s-20260926-1000",
    },
    ...extra,
  };
}

it("answers «quiero estudiar» with its line and one Ir a Estudiar button to the study screen", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-9", kind: "study", summary: "Dar el tema por terminado", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-9", request_id: "req-9", origin: "voice", kind: "study" }));
  });
  const entry = (await screen.findByText(/Pediste: Dar el tema por terminado/)).closest("li") as HTMLElement;
  expect(await within(entry).findByText("Pasando a Estudiar…")).toBeInTheDocument();
  expect(within(entry).queryByRole("link", { name: "Ir a Estudiar" })).toBeNull();

  act(() => {
    stream.push(sseEvent("reply.delta", { turn_id: "turn-9", text: "He cerrado la captura y marcado los apuntes v5 como versión de estudio.", attempt: 1 }));
    stream.push(sseEvent("study.marked", { version: 5, tag: "historia/revolucion-industrial/apuntes-v5" }));
    stream.push(sseEvent("turn.result", studyTurn("turn-9", "req-9")));
  });

  const go = await within(entry).findByRole("link", { name: "Ir a Estudiar" });
  expect(go).toHaveAttribute("href", "/subjects/historia/topics/revolucion-industrial/study");
  expect(within(entry).getByText("He cerrado la captura y marcado los apuntes v5 como versión de estudio.")).toBeInTheDocument();
  // The only control of the turn, and the only one in the chat.
  expect(within(entry).queryAllByRole("button").filter((b) => b.textContent !== "…")).toHaveLength(0);
  expect(within(log()).getAllByRole("link", { name: "Ir a Estudiar" })).toHaveLength(1);
});

it("shows no Ir a Estudiar button on other turns, nor on a study turn without a usable action", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => {
    stream.push(detected("req-1", "Una tabla con las tres causas"));
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("turn.result", voiceResult("turn-1", "req-1", { action: { kind: "go_study", path: "/x" } })));
    stream.push(sseEvent("request.detected", { request_id: "req-2", kind: "study", summary: "A estudiar", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-2", request_id: "req-2", origin: "voice", kind: "study" }));
    stream.push(sseEvent("turn.result", studyTurn("turn-2", "req-2", { action: { kind: "go_study", path: "https://example.com/" } })));
  });

  expect(await screen.findByText("He puesto una tabla con las tres causas.")).toBeInTheDocument();
  expect(await screen.findByText("He cerrado la captura y marcado los apuntes v5 como versión de estudio.")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Ir a Estudiar" })).toBeNull();
});

// ---- #412: the log follows the newest turn, only the latest turn is announced ----

/** Gives the log a layout (jsdom has none): its height, its content's height and a spied scrollTop. */
function layOut(element: HTMLElement, { height = 200, content = 1000 } = {}) {
  let top = 0;
  const sets = vi.fn((value: number) => {
    top = value;
  });
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => height });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => content });
  Object.defineProperty(element, "scrollTop", { configurable: true, get: () => top, set: sets });
  return {
    sets,
    grow: (by: number) => {
      content += by;
    },
    scrollTo: (value: number) => {
      top = value;
      fireEvent.scroll(element);
    },
  };
}

it("scrolls the log to the newest turn as turns arrive and a reply streams", async () => {
  const { opened } = setup();
  const stream = await opened();
  const area = layOut(log());
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  await screen.findByText(/Pediste: Una tabla/);
  await waitFor(() => expect(area.sets).toHaveBeenLastCalledWith(1000));

  area.sets.mockClear();
  area.grow(300);
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "He puesto ", attempt: 1 }));
  });
  await screen.findByText("He puesto");
  await waitFor(() => expect(area.sets).toHaveBeenLastCalledWith(1300));
  expect(screen.queryByRole("button", { name: "Nuevos mensajes ↓" })).toBeNull();
});

it("does not scroll when the student scrolled up, and offers «Nuevos mensajes ↓» instead", async () => {
  const { opened } = setup();
  const stream = await opened();
  const area = layOut(log());
  act(() => stream.push(detected("req-1", "Primera petición")));
  await screen.findByText(/Primera petición/);
  // The student reads further up.
  area.scrollTo(100);
  area.sets.mockClear();

  area.grow(200);
  act(() => stream.push(detected("req-2", "Segunda petición")));
  await screen.findByText(/Segunda petición/);
  expect(area.sets).not.toHaveBeenCalled();
  const follow = await screen.findByRole("button", { name: "Nuevos mensajes ↓" });

  fireEvent.click(follow);
  expect(area.sets).toHaveBeenLastCalledWith(1200);
  expect(screen.queryByRole("button", { name: "Nuevos mensajes ↓" })).toBeNull();

  // Back at the end, the log follows again by itself.
  area.sets.mockClear();
  area.grow(100);
  act(() => stream.push(detected("req-3", "Tercera petición")));
  await screen.findByText(/Tercera petición/);
  await waitFor(() => expect(area.sets).toHaveBeenLastCalledWith(1300));
});

it("announces only the latest turn, never the history", async () => {
  const { opened } = setup({
    [CHAT]: jsonResponse(history([turn({ turn_id: "turn-old", message: "Pon un ejemplo", reply: "Añadido un ejemplo." })], true)),
  });
  const latest = screen.getByTestId("ws-chat-latest");
  expect(latest).toHaveAttribute("aria-live", "polite");
  expect(await screen.findByText("Añadido un ejemplo.")).toBeInTheDocument();
  expect(latest).toBeEmptyDOMElement();

  const stream = await opened();
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: En cola…$/));
  act(() => {
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-1", text: "He puesto ", attempt: 1 }));
  });
  // A streamed fragment is not read out: the status is, until the reply is complete.
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: El asistente está pensando…$/));
  act(() => stream.push(sseEvent("turn.result", voiceResult("turn-1", "req-1"))));
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: He puesto una tabla con las tres causas\.$/));
  expect(latest).not.toHaveTextContent("Añadido un ejemplo.");
});

it("invites speaking only while a capture is running", async () => {
  const { opened, setCapturing } = setup({}, { capturing: true });
  await opened();
  const input = screen.getByLabelText("Mensaje para el asistente");
  expect(input).toHaveAttribute("placeholder", "Escribe o habla: «pon un ejemplo aquí»…");
  expect(await screen.findByText(/hablando o escribiendo/)).toBeInTheDocument();

  setCapturing(false);
  expect(input).toHaveAttribute("placeholder", "Escribe: «pon un ejemplo aquí»…");
  expect(screen.queryByText(/habla/)).toBeNull();
});

it("describes the … of a spoken request in Spanish", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  const more = await screen.findByRole("button", { name: "Ver lo que dijiste" });
  expect(more).toHaveAccessibleDescription("Muestra la transcripción de lo que dijiste y cuándo lo dijiste");
});
