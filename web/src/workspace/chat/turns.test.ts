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
