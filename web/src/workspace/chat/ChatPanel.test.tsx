import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DIFF, history, revision, turn } from "../../chat/testChat";
import { PAGE_TEST_TIMEOUT } from "../../test/timeouts";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../../test/mockApi";
import { type WorkspaceState, WorkspaceContext } from "../state";
import WorkspaceChatSlot from "../WorkspaceChatSlot";
import { installSpeechRecognitionFake, type SpeechFakes } from "../../capture/testing/speech";
import { clock } from "./ChatPanel";
import { type SelectedSource, type SourceSelection, SourceSelectionContext, useSourceSelection } from "../resources/selection";

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

function setup(
  routes: Record<string, Response | (() => Response | Promise<Response>)> = {},
  { capturing = false, quietMs, workspace = {} }: { capturing?: boolean; quietMs?: number; workspace?: Partial<WorkspaceState> } = {},
) {
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
    doubtMarks: null,
    showDoubt: vi.fn(async () => undefined),
    showNextDoubt: vi.fn(async () => undefined),
    showingDoubt: false,
    doubtProblem: null,
    ...workspace,
  };
  const view = (now: boolean) => (
    <WorkspaceContext.Provider value={state}>
      <WorkspaceChatSlot onOpenSource={onOpenSource} retryDelays={[5]} quietMs={quietMs} capturing={now} />
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
  expect(await within(entry).findByText("Respondiendo…")).toBeInTheDocument();

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
}, PAGE_TEST_TIMEOUT);

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
  expect(await within(items[0]).findByText("Respondiendo…")).toBeInTheDocument();
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
  expect(await within(log()).findByText("Respondiendo…")).toBeInTheDocument();

  await act(async () =>
    answer.resolve(
      posted([
        typedRequest("req-t1", "edit", "Un ejemplo", "Pon un ejemplo y aparta la 9"),
        typedRequest("req-t2", "set_aside", "Apartar la página 9", "Pon un ejemplo y aparta la 9", { targets: ["sources/notes/page-009.jpg"] }),
      ]),
    ),
  );
  expect(await within(log()).findAllByText("En cola…")).toHaveLength(2);
  expect(screen.queryByText("Respondiendo…")).toBeNull();
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
  expect(within(entry).getByText("Han surgido 2 dudas: las tienes marcadas en los apuntes.")).toBeInTheDocument();
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
  kind: "illegible",
  text: "Palabra dudosa en la página 3.",
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
  expect(input).toHaveAttribute("placeholder", "Chatea con el asistente…");

  act(() => stream.push(sseEvent("doubt.asked", ASKED)));
  const doubt = (await screen.findByText(ASKED.question)).closest("li") as HTMLElement;
  expect(doubt).toHaveClass("ws-chat-entry-doubt");
  expect(within(doubt).getByText("Duda")).toBeInTheDocument();
  // Its explanation (#516), then the question.
  expect(within(doubt).getByText("Palabra dudosa en la página 3.")).toHaveClass("ws-chat-explanation");
  const suggestions = within(doubt).getByRole("list", { name: "Sugerencias" });
  expect(suggestions.tagName).toBe("OL");
  expect(within(suggestions).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["escrita", "escrito"]);
  expect(within(doubt).getByRole("button", { name: "Ver la fuente: página 3" })).toBeInTheDocument();
  // The suggestions are buttons too (#516); here it is answered by typing.
  expect(within(suggestions).getAllByRole("button").map((b) => b.textContent)).toEqual(["escrita", "escrito"]);
  expect(input).toHaveAttribute("placeholder", "Responde a la duda (escribiendo o con el micrófono) o pide otra cosa…");
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
  expect(input).toHaveAttribute("placeholder", "Chatea con el asistente…");
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
    "«1760» Página 3",
    "«1780» Página 83 del libro",
  ]);
  expect(within(options).getByRole("button", { name: "Es correcto: «1780» (página 83 del libro)" })).toBeEnabled();
  fireEvent.click(within(options).getByRole("button", { name: "Ver la fuente: página 83 del libro" }));
  expect(onOpenSource).toHaveBeenCalledWith(
    "recurso-book-page-083.jpg",
    expect.any(HTMLElement),
    "[Libro, página 83](../sources/book/page-083.jpg)",
  );
  // The refs the options already name are not repeated.
  expect(screen.queryByText(/Sobre:/)).toBeNull();
});

it("answers a doubt by pressing a suggestion, through the doubts route (#516)", async () => {
  const ANSWER = `${BASE}/doubts/p-000004/answer`;
  const { opened, calls } = setup({
    [`POST ${ANSWER}`]: jsonResponse({ pending_id: "p-000004", status: "resolved", resolution: "Pone «escrito».", notes_changed: true, warning: null }),
  });
  const stream = await opened();
  act(() => stream.push(sseEvent("doubt.asked", ASKED)));
  const doubt = (await screen.findByText(ASKED.question)).closest("li") as HTMLElement;
  expect(within(doubt).getByText(/Pulsa una respuesta o contesta escribiendo o con el micrófono/)).toBeInTheDocument();
  fireEvent.click(within(doubt).getByRole("button", { name: "escrito" }));
  await waitFor(() => expect(calls(ANSWER, "POST")).toHaveLength(1));
  const [, init] = calls(ANSWER, "POST")[0];
  expect(JSON.parse((init as RequestInit).body as string)).toEqual({ suggestion: 2, confirm_over_cap: false });
  act(() => stream.push(sseEvent("doubt.resolved", { pending_id: "p-000004", status: "resolved", resolution: "Pone «escrito».", notes_changed: true })));
  expect(await within(doubt).findByText("Respondida: Pone «escrito».")).toBeInTheDocument();
  expect(within(doubt).getByText("Respondiste: «escrito»")).toBeInTheDocument();
  expect(within(doubt).getByRole("button", { name: "escrita" })).toBeDisabled();
});

it("says why a pressed answer was refused, and picks a contradiction's source by its button", async () => {
  const ANSWER = `${BASE}/doubts/p-000009/answer`;
  let refuse = true;
  const { opened, calls } = setup({
    [`POST ${ANSWER}`]: () =>
      refuse
        ? jsonResponse({ detail: "El editor ya está trabajando en los apuntes o las dudas de este tema." }, 409)
        : jsonResponse({ pending_id: "p-000009", status: "resolved", resolution: "Es 1780.", notes_changed: true, warning: null }),
  });
  const stream = await opened();
  act(() =>
    stream.push(
      sseEvent("doubt.asked", {
        pending_id: "p-000009",
        kind: "contradiction",
        text: "Tus apuntes y el libro no coinciden en el año.",
        question: "¿Cuál es el año?",
        suggestions: [],
        options: [
          { source_id: "sources/notes/page-003.jpg", says: "1760" },
          { source_id: "sources/book/page-083.jpg", says: "1780" },
        ],
        refs: ["sources/notes/page-003.jpg", "sources/book/page-083.jpg"],
      }),
    ),
  );
  const book = await screen.findByRole("button", { name: "Es correcto: «1780» (página 83 del libro)" });
  fireEvent.click(book);
  expect(await screen.findByText(/No se pudo aplicar tu respuesta: El editor ya está trabajando/)).toBeInTheDocument();
  refuse = false;
  fireEvent.click(book);
  await waitFor(() => expect(calls(ANSWER, "POST")).toHaveLength(2));
  const [, init] = calls(ANSWER, "POST")[1];
  expect(JSON.parse((init as RequestInit).body as string)).toEqual({ source_id: "sources/book/page-083.jpg", confirm_over_cap: false });
  await waitFor(() => expect(screen.queryByText(/No se pudo aplicar tu respuesta/)).toBeNull());
});

it("shows the doubts marked in the notes as one line with «Siguiente duda.» (#516)", async () => {
  const showNextDoubt = vi.fn(async () => undefined);
  const marks = (count: number) => ({
    count,
    marks: Array.from({ length: count }, (_, i) => ({
      pendingId: `d-${i}`,
      kind: "illegible",
      text: "",
      level: "top" as const,
      blocks: [],
      section: null,
      asked: false,
    })),
  });
  setup({}, { workspace: { doubtMarks: marks(3), showNextDoubt } });
  const line = await screen.findByText("Tienes 3 dudas marcadas en los apuntes");
  expect(line.closest("[role=status]")).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Siguiente duda." }));
  expect(showNextDoubt).toHaveBeenCalledTimes(1);
  cleanup();
  setup({}, { workspace: { doubtMarks: marks(1), doubtProblem: "No se pudo mostrar la duda: Esa duda ya está cerrada." } });
  expect(await screen.findByText("Tienes 1 duda marcada en los apuntes")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Esa duda ya está cerrada.");
  cleanup();
  setup({}, { workspace: { doubtMarks: marks(0) } });
  await screen.findByRole("log");
  expect(screen.queryByRole("button", { name: "Siguiente duda." })).toBeNull();
});

it("moves a doubt shown again to the end of the chat, and a marks event re-reads the marks", async () => {
  const { opened, doubtsChanged } = setup();
  const stream = await opened();
  act(() => stream.push(sseEvent("doubt.asked", ASKED)));
  await screen.findByText(ASKED.question);
  act(() => stream.push(sseEvent("doubts.auto_resolved", { pending_ids: ["p-000001"], summary: "He resuelto 1 duda con tus fuentes." })));
  await screen.findByText("He resuelto 1 duda con tus fuentes.");
  act(() => stream.push(sseEvent("doubt.asked", ASKED)));
  await waitFor(() => {
    const items = within(log()).getAllByRole("listitem").filter((li) => li.parentElement === log());
    expect(items[items.length - 1]).toHaveTextContent(ASKED.question);
  });
  expect(screen.getAllByText(ASKED.question)).toHaveLength(1);
  const before = doubtsChanged.mock.calls.length;
  act(() => stream.push(sseEvent("doubts.marked", { count: 2 })));
  await waitFor(() => expect(doubtsChanged).toHaveBeenCalledTimes(before + 1));
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
  expect(screen.getByLabelText("Mensaje para el asistente")).toHaveAttribute("placeholder", "Chatea con el asistente…");
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

it("marks a typed turn that recorded app feedback with «Bug apuntado», live, and no other turn (#472)", async () => {
  const { opened } = setup({
    [CHAT]: jsonResponse(history([turn({ turn_id: "turn-old" })])),
  });
  expect(await screen.findByText("Pon un ejemplo")).toBeInTheDocument();
  const stream = await opened();
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-5", kind: "question", summary: "Un fallo del botón Hablar", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-5", request_id: "req-5", origin: "voice", kind: "revise" }));
  });
  const entry = (await screen.findByText(/Pediste: Un fallo del botón Hablar/)).closest("li") as HTMLElement;
  expect(within(entry).queryByTestId("feedback-chip")).toBeNull();

  act(() =>
    stream.push(
      sseEvent(
        "turn.result",
        voiceResult("turn-5", "req-5", {
          reply: "He apuntado el bug: El botón Hablar no responde.",
          applied: false,
          summary: null,
          notes_changed: false,
          changed_sections: [],
          diff: "",
          commit: null,
          feedback: { id: "fb-3", kind: "bug", title: "El botón Hablar no responde" },
        }),
      ),
    ),
  );
  const chip = await within(entry).findByTestId("feedback-chip");
  expect(chip).toHaveTextContent(/^Bug apuntado/);
  expect(chip).toHaveAttribute("title", "El botón Hablar no responde");
  expect(within(entry).queryByText(/Cambio aplicado/)).toBeNull();
  expect(within(log()).getAllByTestId("feedback-chip")).toHaveLength(1);
});

it("keeps the «Mejora apuntada» chip of a history turn after a reload (#472)", async () => {
  setup({
    [CHAT]: jsonResponse(
      history([
        turn({ turn_id: "turn-old" }),
        turn({
          turn_id: "turn-fb",
          message: "Apunta una mejora: exportar a PDF",
          reply: "He apuntado la mejora: Exportar los apuntes a PDF.",
          applied: false,
          summary: null,
          changed_sections: [],
          commit: null,
          feedback: { id: "fb-1", kind: "mejora", title: "Exportar los apuntes a PDF" },
        }),
      ]),
    ),
  });
  const entry = (await screen.findByText("Apunta una mejora: exportar a PDF")).closest("li") as HTMLElement;
  expect(within(entry).getByTestId("feedback-chip")).toHaveTextContent(/^Mejora apuntada/);
  expect(within(log()).getAllByTestId("feedback-chip")).toHaveLength(1);
});

it("names the SVG diagram a turn drew and opens it in Recursos (#511)", async () => {
  const { onOpenSource } = setup({
    [CHAT]: jsonResponse(
      history([
        turn({
          turn_id: "turn-diagram",
          message: "Hazme un dibujo del triángulo",
          reply: "He añadido un diagrama del triángulo.",
          crop: { source: "", region: "Triángulo", kind: "diagram", source_id: "sources/images/img-003.svg", path: "x" },
        }),
      ]),
    ),
  });
  const entry = (await screen.findByText("Hazme un dibujo del triángulo")).closest("li") as HTMLElement;
  const line = within(entry).getByText(/Diagrama añadido:/);
  fireEvent.click(within(line).getByRole("button", { name: "Ver la fuente: diagrama 3" }));
  expect(within(line).getByRole("button")).toHaveTextContent("Diagrama 3");
  expect(onOpenSource).toHaveBeenCalledWith("recurso-images-img-003.svg", expect.anything(), expect.stringContaining("../sources/images/img-003.svg"));
  expect(within(entry).queryByText(/Recorte añadido/)).toBeNull();
});

it("names the image a turn cropped from a page and opens it in Recursos, live and after a reload (#493)", async () => {
  const { opened, onOpenSource } = setup({
    [CHAT]: jsonResponse(
      history([
        turn({
          turn_id: "turn-crop",
          message: "Pon solo el diagrama de la página 3",
          reply: "He añadido el recorte del diagrama de la página 3.",
          crop: { source: "sources/notes/page-003.jpg", region: "el diagrama", source_id: "sources/images/img-002.jpg", path: "x" },
        }),
        turn({
          turn_id: "turn-failed",
          message: "Recorta la tabla de la foto",
          reply: "No he podido añadir el recorte: la imagen está borrosa.",
          applied: false,
          summary: null,
          changed_sections: [],
          commit: null,
          crop: { source: "sources/notes/page-004.jpg", region: "la tabla", error: "la imagen está borrosa." },
        }),
      ]),
    ),
  });
  const entry = (await screen.findByText("Pon solo el diagrama de la página 3")).closest("li") as HTMLElement;
  const line = within(entry).getByText(/Recorte añadido:/);
  fireEvent.click(within(line).getByRole("button", { name: "Ver la fuente: imagen recortada 2" }));
  expect(within(line).getByRole("button")).toHaveTextContent("Imagen recortada 2");
  expect(onOpenSource).toHaveBeenCalledWith("recurso-images-img-002.jpg", expect.anything(), expect.stringContaining("../sources/images/img-002.jpg"));
  const failed = (await screen.findByText("Recorta la tabla de la foto")).closest("li") as HTMLElement;
  expect(within(failed).queryByText(/Recorte añadido/)).toBeNull();

  const stream = await opened();
  act(() => {
    stream.push(sseEvent("request.detected", { request_id: "req-7", kind: "edit", summary: "Recortar la tabla", transcript: TRANSCRIPT }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-7", request_id: "req-7", origin: "voice", kind: "revise" }));
  });
  const live = (await screen.findByText(/Pediste: Recortar la tabla/)).closest("li") as HTMLElement;
  expect(within(live).queryByText(/Recorte añadido/)).toBeNull();
  act(() =>
    stream.push(
      sseEvent(
        "turn.result",
        voiceResult("turn-7", "req-7", {
          reply: "He añadido el recorte de la tabla.",
          crop: { source: "sources/notes/page-004.jpg", region: "la tabla", source_id: "sources/images/img-003.jpg", path: "y" },
        }),
      ),
    ),
  );
  expect(await within(live).findByText(/Recorte añadido:/)).toHaveTextContent("Recorte añadido: Imagen recortada 3");
});

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
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: Respondiendo…$/));
  act(() => stream.push(sseEvent("turn.result", voiceResult("turn-1", "req-1"))));
  await waitFor(() => expect(latest).toHaveTextContent(/^Asistente: He puesto una tabla con las tres causas\.$/));
  expect(latest).not.toHaveTextContent("Añadido un ejemplo.");
});

it("invites speaking only while a capture is running", async () => {
  const { opened, setCapturing } = setup({}, { capturing: true });
  await opened();
  const input = screen.getByLabelText("Mensaje para el asistente");
  expect(input).toHaveAttribute("placeholder", "Chatea con el asistente…");
  expect(await screen.findByText(/hablando o escribiendo/)).toBeInTheDocument();

  setCapturing(false);
  expect(input).toHaveAttribute("placeholder", "Chatea con el asistente…");
  expect(screen.queryByText(/hablando/)).toBeNull();
  expect(screen.getByText(/escribiendo o con el micrófono/)).toBeInTheDocument();
});

it("describes the … of a spoken request in Spanish", async () => {
  const { opened } = setup();
  const stream = await opened();
  act(() => stream.push(detected("req-1", "Una tabla con las tres causas")));
  const more = await screen.findByRole("button", { name: "Ver lo que dijiste" });
  expect(more).toHaveAccessibleDescription("Muestra la transcripción de lo que dijiste y cuándo lo dijiste");
});

describe("the microphone button (#428)", () => {
  let speech: SpeechFakes;
  beforeEach(() => {
    speech = installSpeechRecognitionFake();
  });
  afterEach(() => speech.restore());

  const SPEAK = "Dictar el mensaje por voz";
  const LISTENING = "Escuchando… (pulsa para parar)";

  it("sends a spoken message once, showing the interim text in the input", async () => {
    const { opened, calls } = setup({ [`POST ${MESSAGES}`]: () => posted([typedRequest("req-t1", "edit", "Un ejemplo", "Pon un ejemplo")]) });
    await opened();
    const input = screen.getByLabelText("Mensaje para el asistente");
    fireEvent.click(screen.getByRole("button", { name: SPEAK }));
    expect(screen.getByRole("button", { name: LISTENING })).toHaveAttribute("aria-pressed", "true");
    const recognition = speech.recognitions[0];
    act(() => recognition.emitResult([{ transcript: "Pon un" }]));
    expect(input).toHaveValue("Pon un");
    act(() => recognition.emitResult([{ transcript: "Pon un ejemplo", final: true }]));
    act(() => recognition.emitEnd());
    await waitFor(() => expect(calls(MESSAGES, "POST")).toHaveLength(1));
    expect(JSON.parse(String((calls(MESSAGES, "POST")[0][1] as RequestInit).body))).toEqual({ text: "Pon un ejemplo." });
    expect(input).toHaveValue("");
    expect(screen.getByRole("button", { name: SPEAK })).toHaveAttribute("aria-pressed", "false");
  });

  it("stops listening when pressed again, and says why nothing came out", async () => {
    const { opened, calls } = setup();
    await opened();
    fireEvent.click(screen.getByRole("button", { name: SPEAK }));
    fireEvent.click(screen.getByRole("button", { name: LISTENING }));
    const recognition = speech.recognitions[0];
    expect(recognition.stopCount).toBe(1);
    act(() => recognition.emitError("not-allowed"));
    act(() => recognition.emitEnd());
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay permiso para usar el micrófono");
    expect(calls(MESSAGES, "POST")).toHaveLength(0);
  });

  it("is hidden while a capture runs, and a capture starting stops its recognition", async () => {
    const { opened, setCapturing } = setup({}, { capturing: true });
    await opened();
    expect(screen.queryByRole("button", { name: SPEAK })).toBeNull();
    setCapturing(false);
    fireEvent.click(await screen.findByRole("button", { name: SPEAK }));
    setCapturing(true);
    expect(screen.queryByRole("button", { name: SPEAK })).toBeNull();
    expect(screen.queryByRole("button", { name: LISTENING })).toBeNull();
    expect(speech.recognitions[0].stopCount).toBe(1);
  });

  it("stops a running recognition when the panel unmounts", async () => {
    const { opened } = setup();
    await opened();
    fireEvent.click(screen.getByRole("button", { name: SPEAK }));
    cleanup();
    expect(speech.recognitions[0].stopCount).toBe(1);
  });
});

it("offers a disabled «Hablar» with a hint in a browser without speech recognition", async () => {
  const { opened } = setup();
  await opened();
  const speak = screen.getByRole("button", { name: "Dictar el mensaje por voz" });
  expect(speak).toBeDisabled();
  expect(speak).not.toHaveTextContent("Hablar");
  expect(speak).toHaveAccessibleDescription("Este navegador no reconoce la voz: escribe tu mensaje.");
  const input = screen.getByLabelText("Mensaje para el asistente");
  fireEvent.change(input, { target: { value: "Pon un ejemplo" } });
  expect(screen.getByRole("button", { name: "Enviar" })).toBeEnabled();
});

describe("the Recursos selection as chips (#432)", () => {
  const page = (n: number): SelectedSource => ({ id: `sources/notes/page-00${n}.jpg`, title: `Pág. ${n}` });

  function setupSelected(sources: SelectedSource[]) {
    const fetchMock = stubApi({
      [STREAM]: () => streamResponse().response,
      [CHAT]: jsonResponse(history()),
      [`POST ${MESSAGES}`]: () => posted([typedRequest("req-t1", "incorporate", "Incorporar", "incorpora estas")]),
    });
    const state: WorkspaceState = {
      subjectId: "historia",
      topicId: "revolucion-industrial",
      notes: { kind: "empty" },
      changedSections: new Set(),
      reloadNotes: vi.fn(async () => undefined),
      doubtsKey: 0,
      doubtsChanged: vi.fn(),
      doubtMarks: null,
      showDoubt: vi.fn(async () => undefined),
      showNextDoubt: vi.fn(async () => undefined),
      showingDoubt: false,
      doubtProblem: null,
    };
    const held: { current: SourceSelection | null } = { current: null };
    function Harness() {
      const selection = useSourceSelection();
      held.current = selection;
      return (
        <WorkspaceContext.Provider value={state}>
          <SourceSelectionContext.Provider value={selection}>
            <WorkspaceChatSlot onOpenSource={vi.fn()} retryDelays={[5]} />
          </SourceSelectionContext.Provider>
        </WorkspaceContext.Provider>
      );
    }
    render(<Harness />);
    act(() => {
      for (const source of sources) held.current!.toggle(source, sources, false);
    });
    const posts = () => fetchMock.mock.calls.filter(([input, init]) => input === MESSAGES && (init as RequestInit | undefined)?.method === "POST");
    return { held, posts };
  }

  const chips = () => screen.getByRole("group", { name: "Fuentes seleccionadas para el mensaje" });

  it("shows one chip per selected source, and × deselects it", () => {
    const { held } = setupSelected([page(3), page(4)]);
    expect(within(chips()).getAllByRole("listitem").map((li) => li.firstChild?.textContent)).toEqual(["Pág. 3", "Pág. 4"]);
    fireEvent.click(screen.getByRole("button", { name: "Quitar Pág. 3 de la selección" }));
    expect(held.current!.selected.map((s) => s.id)).toEqual(["sources/notes/page-004.jpg"]);
    expect(within(chips()).getAllByRole("listitem")).toHaveLength(1);
    fireEvent.click(within(chips()).getByRole("button", { name: "Quitar selección" }));
    expect(screen.queryByRole("group", { name: "Fuentes seleccionadas para el mensaje" })).toBeNull();
  });

  it("shows the first five and «+N» past six", () => {
    setupSelected([1, 2, 3, 4, 5, 6, 7].map(page));
    const items = within(chips()).getAllByRole("listitem");
    expect(items.map((li) => li.firstChild?.textContent)).toEqual(["Pág. 1", "Pág. 2", "Pág. 3", "Pág. 4", "Pág. 5", "+2"]);
    expect(items[5]).toHaveAttribute("title", "Pág. 6, Pág. 7");
  });

  it("shows six chips when exactly six are selected", () => {
    setupSelected([1, 2, 3, 4, 5, 6].map(page));
    expect(within(chips()).getAllByRole("listitem")).toHaveLength(6);
    expect(within(chips()).queryByText(/^\+/)).toBeNull();
  });

  it("sends the selected source ids with the message and keeps the selection", async () => {
    const { posts } = setupSelected([page(3), page(1)]);
    const input = screen.getByLabelText("Mensaje para el asistente");
    fireEvent.change(input, { target: { value: "incorpora el texto de estas" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(String((posts()[0][1] as RequestInit).body))).toEqual({
      text: "incorpora el texto de estas",
      selected_source_ids: ["sources/notes/page-003.jpg", "sources/notes/page-001.jpg"],
    });
    expect(await within(log()).findByText("En cola…")).toBeInTheDocument();
    expect(within(chips()).getAllByRole("listitem")).toHaveLength(2);
  });

  it("sends only the text when nothing is selected", async () => {
    const { posts } = setupSelected([]);
    expect(screen.queryByRole("group", { name: "Fuentes seleccionadas para el mensaje" })).toBeNull();
    const input = screen.getByLabelText("Mensaje para el asistente");
    fireEvent.change(input, { target: { value: "Pon un ejemplo" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(String((posts()[0][1] as RequestInit).body))).toEqual({ text: "Pon un ejemplo" });
  });
});

it("shows the sent message at once with «Respondiendo…», and reads the history when the stream stays silent (#452)", async () => {
  const answer = deferred<Response>();
  let answered = false;
  const { opened, calls } = setup(
    {
      [`POST ${MESSAGES}`]: () => answer.promise,
      [CHAT]: () =>
        jsonResponse(
          history(
            answered
              ? [turn({ time: new Date().toISOString(), turn_id: "turn-9", origin: "typed", message: "transcribe esta página", reply: "Ya está transcrita.", applied: false, summary: null })]
              : [],
          ),
        ),
    },
    { quietMs: 40 },
  );
  await opened();
  await waitFor(() => expect(calls(CHAT)).toHaveLength(1));

  await type("transcribe esta página");
  const input = screen.getByLabelText("Mensaje para el asistente");
  // In the history at once, as the student's message, with the spinner; the input is empty.
  const entry = within(log()).getByText("transcribe esta página").closest("li") as HTMLElement;
  expect(within(entry).getByText("Escribiste")).toBeInTheDocument();
  expect(within(entry).getByText("Respondiendo…")).toBeInTheDocument();
  expect(within(entry).getByTestId("ws-chat-spinner")).toBeInTheDocument();
  expect(input).toHaveValue("");

  await act(async () => answer.resolve(posted([typedRequest("req-t1", "edit", "Transcribir", "transcribe esta página")])));
  expect(await within(log()).findByText("En cola…")).toBeInTheDocument();
  expect(within(log()).getByTestId("ws-chat-spinner")).toBeInTheDocument();

  // The turn ran, but the stream said nothing: the history is read again and shows it.
  answered = true;
  expect(await within(log()).findByText("Ya está transcrita.", {}, { timeout: 3000 })).toBeInTheDocument();
  expect(within(log()).queryByTestId("ws-chat-spinner")).toBeNull();
  expect(within(log()).getAllByText("transcribe esta página")).toHaveLength(1);
}, PAGE_TEST_TIMEOUT);

it("keeps a message the server refused in the chat with a Spanish error, and the input free", async () => {
  const { opened } = setup({ [`POST ${MESSAGES}`]: jsonResponse({ detail: "El asistente no está disponible." }, 503) });
  await opened();
  await type("hola");
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo completar: El asistente no está disponible.");
  expect(within(log()).getByText("hola")).toBeInTheDocument();
  expect(within(log()).queryByTestId("ws-chat-spinner")).toBeNull();
}, PAGE_TEST_TIMEOUT);
