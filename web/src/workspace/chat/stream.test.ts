import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../../test/mockApi";
import { readWorkspaceEvent, type WorkspaceEvent } from "./api";
import { connectWorkspaceStream } from "./stream";

const STREAM = "/api/subjects/historia/topics/revolucion-industrial/workspace/stream";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("decodes the events it knows and ignores the rest", () => {
  expect(readWorkspaceEvent("turn.started", '{"turn_id":"turn-1","request_id":"req-1","origin":"voice","kind":"revise"}')).toEqual({
    type: "turn.started",
    turnId: "turn-1",
    requestId: "req-1",
    origin: "voice",
    kind: "revise",
  });
  expect(readWorkspaceEvent("turn.error", '{"turn_id":"turn-1","status":409,"detail":"Límite.","code":"cost_cap_reached"}')).toMatchObject({
    type: "turn.error",
    overCap: true,
    detail: "Límite.",
  });
  expect(readWorkspaceEvent("something.new", '{"turn_id":"turn-1"}')).toBeNull();
  expect(readWorkspaceEvent("reply.delta", "not json")).toBeNull();
  expect(readWorkspaceEvent("reply.delta", '{"text":"sin turno"}')).toBeNull();
});

it("retries with backoff until the stream opens, then reports each reopening", async () => {
  vi.useFakeTimers();
  const streams: ReturnType<typeof streamResponse>[] = [];
  let answers = 0;
  stubApi({
    [STREAM]: () => {
      answers += 1;
      if (answers === 1) return jsonResponse({ detail: "No se pudo abrir la bóveda." }, 503);
      const stream = streamResponse();
      streams.push(stream);
      return stream.response;
    },
  });
  const events: WorkspaceEvent[] = [];
  const opens: boolean[] = [];
  let downs = 0;
  const close = connectWorkspaceStream("historia", "revolucion-industrial", {
    retryDelays: [100, 1000],
    onEvent: (event) => events.push(event),
    onOpen: (reconnected) => opens.push(reconnected),
    onDown: () => (downs += 1),
  });

  await vi.advanceTimersByTimeAsync(0);
  expect(downs).toBe(1);
  expect(opens).toEqual([]);
  await vi.advanceTimersByTimeAsync(99);
  expect(answers).toBe(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(opens).toEqual([false]);

  streams[0].push(sseEvent("notes.changed", { revision: "a".repeat(64), origin: "user", summary: null }));
  await vi.advanceTimersByTimeAsync(0);
  expect(events).toEqual([{ type: "notes.changed", revision: "a".repeat(64), origin: "user", summary: null, turnId: null }]);

  // A successful open resets the backoff: the next attempt waits the first delay again.
  streams[0].fail();
  await vi.advanceTimersByTimeAsync(0);
  expect(downs).toBe(2);
  await vi.advanceTimersByTimeAsync(100);
  expect(opens).toEqual([false, true]);

  close();
  streams[1].close();
  await vi.advanceTimersByTimeAsync(5000);
  expect(answers).toBe(3);
  expect(downs).toBe(2);
});
