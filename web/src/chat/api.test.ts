import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, sseResponse, streamResponse, stubApi } from "../test/mockApi";
import { describeChatFailure, fetchChatHistory, sendChatMessage, undoLastTurn } from "./api";
import { history, revision, turn } from "./testChat";

afterEach(() => {
  vi.unstubAllGlobals();
});

const CHAT = "/api/subjects/historia/topics/revolucion-industrial/notes/chat";

it("posts the message and streams the reply, dropping it on a restart", async () => {
  const fetchMock = stubApi({
    [`POST ${CHAT}`]: () =>
      sseResponse([
        ["reply.delta", { text: "Primer ", attempt: 1 }],
        ["reply.restart", { attempt: 2 }],
        ["reply.delta", { text: "Segundo", attempt: 2 }],
        ["result", revision()],
      ]),
  });
  const seen: string[] = [];
  const outcome = await sendChatMessage("historia", "revolucion-industrial", "Esto está demasiado resumido", {
    onDelta: (text, attempt) => seen.push(`${attempt}:${text}`),
    onRestart: (attempt) => seen.push(`restart ${attempt}`),
  });
  expect(seen).toEqual(["1:Primer ", "restart 2", "2:Segundo"]);
  expect(outcome).toMatchObject({ kind: "ok", value: { applied: true, changed_sections: ["contexto"], commit: "abc123" } });
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(init.body as string)).toEqual({ message: "Esto está demasiado resumido", confirm_over_cap: false });
});

it("reports an error before the stream with the backend's detail", async () => {
  stubApi({ [`POST ${CHAT}`]: jsonResponse({ detail: "El editor ya está trabajando en los apuntes." }, 409) });
  const outcome = await sendChatMessage("historia", "revolucion-industrial", "hola");
  expect(outcome).toEqual({ kind: "refused", status: 409, detail: "El editor ya está trabajando en los apuntes.", overCap: false });
});

it("reports an error event, marking a reached cost cap", async () => {
  const detail = "Se ha alcanzado el límite de gasto del día (5.00 de 5.00 USD). Confirma para continuar igualmente.";
  stubApi({ [`POST ${CHAT}`]: () => sseResponse([["error", { status: 409, detail }]]) });
  const outcome = await sendChatMessage("historia", "revolucion-industrial", "hola", { confirmOverCap: true });
  expect(outcome).toEqual({ kind: "refused", status: 409, detail, overCap: true });
});

it("is interrupted when the stream ends or breaks before its result", async () => {
  const stream = streamResponse();
  stubApi({ [`POST ${CHAT}`]: () => stream.response });
  const pending = sendChatMessage("historia", "revolucion-industrial", "hola");
  stream.push(sseEvent("reply.delta", { text: "a", attempt: 1 }));
  stream.fail();
  const outcome = await pending;
  expect(outcome).toEqual({ kind: "interrupted" });
  if (outcome.kind !== "ok") expect(describeChatFailure(outcome)).toContain("Se cortó la conexión");

  stubApi({ [`POST ${CHAT}`]: () => sseResponse([["reply.delta", { text: "a", attempt: 1 }]]) });
  expect(await sendChatMessage("historia", "revolucion-industrial", "hola")).toEqual({ kind: "interrupted" });
});

it("is unreachable when fetch fails", async () => {
  stubApi({ [`POST ${CHAT}`]: new TypeError("down") });
  expect(await sendChatMessage("historia", "revolucion-industrial", "hola")).toEqual({ kind: "unreachable" });
});

it("reads the history and the undo", async () => {
  stubApi({
    [CHAT]: jsonResponse(history([turn()], true)),
    [`POST ${CHAT}/undo`]: jsonResponse({
      subject: "historia",
      topic: "revolucion-industrial",
      undone_commit: "old111",
      summary: "Ejemplo añadido",
      commit: "def456",
      notes_changed: true,
      diff: "",
      notes: "x",
      paths: [],
    }),
  });
  expect(await fetchChatHistory("historia", "revolucion-industrial")).toMatchObject({
    kind: "ok",
    value: { can_undo: true, turns: [{ message: "Pon un ejemplo", commit: "old111", undone: false }] },
  });
  expect(await undoLastTurn("historia", "revolucion-industrial")).toMatchObject({
    kind: "ok",
    value: { undone_commit: "old111", notes_changed: true },
  });
});

it("rejects a history that is not one", async () => {
  stubApi({ [CHAT]: jsonResponse({ turns: [{ message: 1 }] }) });
  expect(await fetchChatHistory("historia", "revolucion-industrial")).toEqual({ kind: "error", status: 200 });
});
