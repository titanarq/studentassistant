import { expect, it } from "vitest";
import { readWorkspaceHistory } from "./api";
import { INITIAL_CHAT, reduceChat } from "./turns";

function historyOf(turns: Record<string, unknown>[]) {
  const history = readWorkspaceHistory({ turns, can_undo: false });
  if (history === null) throw new Error("unreadable history");
  return history;
}

it("drops a queued typed request whose turn the history already has, one for one", () => {
  const time = new Date().toISOString();
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "sí", time });
  state = reduceChat(state, {
    type: "post.done",
    key: "post1",
    result: { kind: "ok", messageId: "msg-00000001", classified: true, requests: [{ requestId: "req-t1", kind: "edit", summary: "Sí", text: "sí", targets: [] }] },
  });
  state = reduceChat(state, { type: "post.start", key: "post2", message: "sí", time });
  state = reduceChat(state, {
    type: "post.done",
    key: "post2",
    result: { kind: "ok", messageId: "msg-00000002", classified: true, requests: [{ requestId: "req-t2", kind: "edit", summary: "Sí", text: "sí", targets: [] }] },
  });
  expect(state.entries.map((e) => e.status)).toEqual(["queued", "queued"]);

  // The first ran while the stream was down: typed turns carry no request id.
  const merged = reduceChat(state, {
    type: "history",
    history: historyOf([{ time, turn_id: "turn-1", origin: "typed", message: "sí", reply: "Hecho.", applied: false }]),
  });
  expect(merged.entries.map((e) => [e.turnId, e.status])).toEqual([
    ["turn-1", "done"],
    [null, "queued"],
  ]);
  expect(merged.entries[1].requestId).toBe("req-t2");
});

it("keeps an older typed turn of the same words from standing for a new request", () => {
  const time = new Date().toISOString();
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "sí", time });
  state = reduceChat(state, {
    type: "post.done",
    key: "post1",
    result: { kind: "ok", messageId: null, classified: true, requests: [{ requestId: "req-t1", kind: "edit", summary: "Sí", text: "sí", targets: [] }] },
  });
  const merged = reduceChat(state, {
    type: "history",
    history: historyOf([{ time: "2026-01-01T10:00:00Z", turn_id: "turn-old", origin: "typed", message: "sí", reply: "Hecho.", applied: false }]),
  });
  expect(merged.entries.map((e) => e.status)).toEqual(["done", "queued"]);
});

it("keeps a failed POST's message with its detail", () => {
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "Hola", time: new Date().toISOString() });
  state = reduceChat(state, { type: "post.done", key: "post1", result: { kind: "failed", detail: "No se pudo conectar con el servidor." } });
  expect(state.entries).toHaveLength(1);
  expect(state.entries[0]).toMatchObject({ status: "failed", message: "Hola", failure: "No se pudo conectar con el servidor." });
});

const typed = (requestId: string, text: string) => ({ requestId, kind: "edit", summary: "Un cambio", text, targets: [] });

it("keeps the student's message when its request id repeats an earlier, finished one (#452)", () => {
  const time = new Date().toISOString();
  // A finished turn of an earlier session's `req-t1` (typed ids restart per session).
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "primero", time });
  state = reduceChat(state, { type: "post.done", key: "post1", result: { kind: "ok", messageId: "msg-1", classified: true, requests: [typed("req-t1", "primero")] } });
  state = reduceChat(state, { type: "event", event: { type: "turn.started", turnId: "turn-1", requestId: "req-t1", origin: "typed", kind: "revise" } });
  state = reduceChat(state, { type: "event", event: { type: "reply.delta", turnId: "turn-1", text: "Hecho.", attempt: 1 } });
  state = reduceChat(state, { type: "event", event: { type: "turn.error", turnId: "turn-1", requestId: "req-t1", status: 500, detail: "x", overCap: false } });
  expect(state.entries[0].status).toBe("failed");

  state = reduceChat(state, { type: "post.start", key: "post2", message: "segundo", time });
  state = reduceChat(state, { type: "event", event: { type: "request.detected", requestId: "req-t1", kind: "edit", summary: "Otro", origin: "typed", transcript: { requestId: "req-t1", text: "segundo", startMs: 0, endMs: 0 } } });
  state = reduceChat(state, { type: "post.done", key: "post2", result: { kind: "ok", messageId: "msg-2", classified: true, requests: [typed("req-t1", "segundo")] } });
  expect(state.entries.map((e) => [e.key, e.message, e.status])).toEqual([
    ["post1", "primero", "failed"],
    ["post2", "segundo", "queued"],
  ]);
  expect(state.entries[0].messageId).toBe("msg-1");

  state = reduceChat(state, { type: "event", event: { type: "turn.started", turnId: "turn-2", requestId: "req-t1", origin: "typed", kind: "revise" } });
  state = reduceChat(state, { type: "event", event: { type: "reply.delta", turnId: "turn-2", text: "Vale.", attempt: 1 } });
  expect(state.entries.map((e) => [e.key, e.turnId, e.reply])).toEqual([
    ["post1", "turn-1", "Hecho."],
    ["post2", "turn-2", "Vale."],
  ]);
});

it("binds a typed message announced on the stream before the POST answers, in place", () => {
  const time = new Date().toISOString();
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "pon un ejemplo", time });
  state = reduceChat(state, { type: "event", event: { type: "request.detected", requestId: "req-t3", kind: "edit", summary: "Un ejemplo", origin: "typed", transcript: { requestId: "req-t3", text: "pon un ejemplo", startMs: 0, endMs: 0 } } });
  expect(state.entries.map((e) => [e.key, e.requestId, e.status])).toEqual([["post1", "req-t3", "queued"]]);
  state = reduceChat(state, { type: "post.done", key: "post1", result: { kind: "ok", messageId: "msg-3", classified: true, requests: [typed("req-t3", "pon un ejemplo")] } });
  expect(state.entries.map((e) => [e.key, e.message, e.status, e.messageId])).toEqual([["post1", "pon un ejemplo", "queued", "msg-3"]]);
});

it("keeps the message, failed in Spanish, when the POST queued no request", () => {
  let state = reduceChat(INITIAL_CHAT, { type: "post.start", key: "post1", message: "Hola", time: new Date().toISOString() });
  state = reduceChat(state, { type: "post.done", key: "post1", result: { kind: "ok", messageId: null, classified: true, requests: [] } });
  expect(state.entries).toHaveLength(1);
  expect(state.entries[0]).toMatchObject({ status: "failed", message: "Hola" });
  expect(state.entries[0].failure).toMatch(/Vuelve a enviarlo/);
});
