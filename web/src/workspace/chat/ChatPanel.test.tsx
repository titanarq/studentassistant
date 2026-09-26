import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DIFF, history, revision, turn } from "../../chat/testChat";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../../test/mockApi";
import { type WorkspaceState, WorkspaceContext } from "../state";
import WorkspaceChatSlot from "../WorkspaceChatSlot";
import { clock } from "./ChatPanel";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const STREAM = `${BASE}/workspace/stream`;
const CHAT = `${BASE}/notes/chat`;

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

function setup(routes: Record<string, Response | (() => Response | Promise<Response>)> = {}) {
  const streams: Stream[] = [];
  const reloadNotes = vi.fn(async () => undefined);
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
  };
  render(
    <WorkspaceContext.Provider value={state}>
      <WorkspaceChatSlot onOpenSource={vi.fn()} retryDelays={[5]} />
    </WorkspaceContext.Provider>,
  );
  const opened = async (count = 1) => {
    await waitFor(() => expect(streams).toHaveLength(count));
    return streams[count - 1];
  };
  const calls = (path: string, method = "GET") =>
    fetchMock.mock.calls.filter(([input, init]) => input === path && ((init as RequestInit | undefined)?.method ?? "GET") === method);
  return { streams, reloadNotes, fetchMock, opened, calls };
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
  expect(log()).toHaveAttribute("aria-live", "polite");

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

it("sends a typed message with Enter and shows its reply once, although the workspace stream broadcasts it too", async () => {
  const post = streamResponse();
  const { opened, calls, reloadNotes } = setup({ [`POST ${CHAT}`]: () => post.response });
  const stream = await opened();

  const input = screen.getByLabelText("Mensaje para el asistente");
  const send = screen.getByRole("button", { name: "Enviar" });
  expect(send).toBeDisabled();
  fireEvent.change(input, { target: { value: "Pon un ejemplo" } });
  expect(send).toBeEnabled();
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(calls(CHAT, "POST")).toHaveLength(0);
  fireEvent.keyDown(input, { key: "Enter" });

  await waitFor(() => expect(calls(CHAT, "POST")).toHaveLength(1));
  expect(JSON.parse(String((calls(CHAT, "POST")[0][1] as RequestInit).body))).toEqual({
    message: "Pon un ejemplo",
    confirm_over_cap: false,
  });
  expect(input).toHaveValue("");
  const entry = (await within(log()).findByText("Pon un ejemplo")).closest("li") as HTMLElement;

  act(() => {
    post.push(sseEvent("reply.delta", { text: "Añado ", attempt: 1 }));
    stream.push(sseEvent("turn.started", { turn_id: "turn-9", request_id: null, origin: "typed", kind: "revise" }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-9", text: "Añado ", attempt: 1 }));
    post.push(sseEvent("reply.delta", { text: "un ejemplo.", attempt: 1 }));
    stream.push(sseEvent("reply.delta", { turn_id: "turn-9", text: "un ejemplo.", attempt: 1 }));
  });
  expect(await within(entry).findByText("Añado un ejemplo.")).toBeInTheDocument();

  const result = revision({ turn_id: "turn-9", message: "Pon un ejemplo", reply: "Añado un ejemplo de Manchester." });
  act(() => {
    stream.push(sseEvent("turn.result", { ...result, request_id: null, kind: "revise" }));
    post.push(sseEvent("result", result));
    post.close();
  });
  expect(await within(entry).findByText("Añado un ejemplo de Manchester.")).toBeInTheDocument();
  await waitFor(() => expect(reloadNotes).toHaveBeenCalledWith(["contexto"]));
  expect(within(log()).getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getAllByText("Añado un ejemplo de Manchester.")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Deshacer el último cambio" })).toBeEnabled();
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

it("shows a turn error in Spanish and offers to go on over the cost cap", async () => {
  const { opened, calls } = setup({
    [`POST ${CHAT}`]: () => sseResponse([["result", voiceResult("turn-2", "req-1")]]),
  });
  const stream = await opened();
  act(() => {
    stream.push(detected("req-1", "Una tabla con las tres causas"));
    stream.push(sseEvent("turn.started", { turn_id: "turn-1", request_id: "req-1", origin: "voice", kind: "revise" }));
    stream.push(
      sseEvent("turn.error", {
        turn_id: "turn-1",
        request_id: "req-1",
        status: 409,
        detail: "Se ha alcanzado el límite de gasto del tema.",
        code: "cost_cap_reached",
      }),
    );
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo completar: Se ha alcanzado el límite de gasto del tema.");

  fireEvent.click(screen.getByRole("button", { name: "Continuar igualmente" }));
  await waitFor(() => expect(calls(CHAT, "POST")).toHaveLength(1));
  expect(JSON.parse(String((calls(CHAT, "POST")[0][1] as RequestInit).body))).toEqual({
    message: TRANSCRIPT.text,
    confirm_over_cap: true,
  });
  expect(await screen.findByText("He puesto una tabla con las tres causas.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(within(log()).getAllByRole("listitem")).toHaveLength(1);
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

it("says so when a typed message is refused before its turn", async () => {
  setup({
    [`POST ${CHAT}`]: jsonResponse({ detail: "Se está preparando el tema; espera a que termine." }, 409),
  });
  fireEvent.change(screen.getByLabelText("Mensaje para el asistente"), { target: { value: "Hola" } });
  fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Se está preparando el tema; espera a que termine.");
  expect(screen.getByText("Hola")).toBeInTheDocument();
});
